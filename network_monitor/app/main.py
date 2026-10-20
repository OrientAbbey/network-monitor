"""
Boucle temps réel (§30) : assemble les modules sans logique métier propre.
Chaque étape correspond explicitement à une étape du §30 pour rester lisible.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from network_monitor.analysis.detection import (
    IsolationForestDetector,
    LossBaseline,
    PersistenceTracker,
    build_anomaly_result,
)
from network_monitor.analysis.features import compute_features, history_steps
from network_monitor.analysis.features_extended import compute_extended_features
from network_monitor.analysis.forecasting import ForecastModel, select_model
from network_monitor.app.config import AppConfig
from network_monitor.app.models import (
    AnomalyResult,
    ForecastResult,
    Incident,
    IncidentStatus,
    MaintenanceWindow,
    RawMeasurement,
    TrafficSample,
)
from network_monitor.app.notify import NotificationDispatcher
from network_monitor.data.source import DataSource, DataValidator
from network_monitor.incidents.manager import (
    Alert,
    IncidentManager,
    incident_worth_persisting,
)
from network_monitor.storage.repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class _TickResults:
    """Résultats d'un tick, écrits ensemble en une seule transaction (§29)."""

    samples: list[TrafficSample] = field(default_factory=list)
    forecasts: list[ForecastResult] = field(default_factory=list)
    anomalies: list[AnomalyResult] = field(default_factory=list)
    incidents: dict[str, Incident] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)


class MonitoringLoop:
    """
    Exécute, à chaque tick, les étapes 1 à 11 du §30 pour toutes les sorties
    connues. Ne connaît aucun détail d'implémentation des modules qu'elle
    orchestre — c'est le rôle de chacun d'eux (§4).
    """

    def __init__(
        self, source: DataSource, storage: Repository, config: AppConfig, notifier: NotificationDispatcher | None = None
    ) -> None:
        self._notifier = notifier
        self._source = source
        self._storage = storage
        self._config = config
        self._validator = DataValidator(source.list_outputs(), source.get_interval())
        forest_cfg = config.detection.isolation_forest
        self._incident_manager = IncidentManager(
            config.incident,
            critical_score=config.alerts.critical_score,
            isolation_confirm_score=forest_cfg.confirm_score if forest_cfg.enabled else 1.01,
        )
        outputs = set(source.list_outputs())
        unknown = {o for pair in config.topology.parents.items() for o in pair} - outputs
        if unknown:
            logger.warning("topology.parents cite des sorties inconnues de la source : %s", sorted(unknown))
        self._incident_manager.set_topology(config.topology.parents, config.topology.correlation_window)
        self._maintenance: list[MaintenanceWindow] = []
        self._incident_manager.set_maintenance_check(self._under_maintenance)
        self._persistence = PersistenceTracker(interval_seconds=source.get_interval().total_seconds())
        self._forecast_models: dict[str, ForecastModel] = {}
        self._loss_baselines: dict[str, LossBaseline] = {}
        self._forests: dict[str, IsolationForestDetector] = {}
        # Mesures précédentes de chaque sortie (1 h), pour les features glissantes (§9).
        self._recent: dict[str, deque[TrafficSample]] = {
            o: deque(maxlen=history_steps(source.get_interval())) for o in source.list_outputs()
        }
        self._last_calibrated_at: datetime | None = None
        self.last_sample_at: dict[str, datetime] = {}
        """Horodatage de la dernière mesure utilisable reçue, par sortie (supervision : données périmées)."""

    def is_calibrated(self, output_id: str) -> bool:
        return output_id in self._forecast_models and output_id in self._loss_baselines

    @property
    def incident_manager(self) -> IncidentManager:
        """Accès public pour reconfiguration à chaud (ex. PUT /config de l'API)."""
        return self._incident_manager

    def forecast_ahead(self, output_id: str, from_time: datetime, horizon: timedelta) -> list[ForecastResult]:
        """
        Prévision à la demande au-delà de `from_time`, sans faire progresser
        la boucle ni rien persister — sert la courbe "prédiction" du
        dashboard (§13), à partir du modèle déjà calibré pour cette sortie.
        """
        model = self._forecast_models.get(output_id)
        if model is None:
            raise ValueError(f"Sortie non calibrée : {output_id}")
        return model.predict(from_time, horizon, self._source.get_interval())

    def calibrate(self, now: datetime) -> None:
        """Calibration initiale (§8) : un modèle et une baseline de pertes par sortie."""
        self._recompute_models(now, exclude_ranges=None)
        self._last_calibrated_at = now

    def maybe_recalibrate(self, now: datetime) -> None:
        """
        Recalibration périodique (§32) : hors du chemin critique de `run_tick`,
        ne s'exécute que si `recalibration.interval` s'est écoulé depuis la
        dernière calibration. Les plages couvertes par un incident actif sont
        exclues de l'historique réajusté, pour ne pas apprendre l'anomalie en
        cours comme un nouveau comportement normal.
        """
        cfg = self._config.recalibration
        if not cfg.enabled or self._last_calibrated_at is None:
            return
        # Une sortie sans modèle (historique encore insuffisant au démarrage) est réessayée
        # chaque heure : sans cela, elle resterait sans détection jusqu'à la recalibration suivante.
        uncalibrated = any(o not in self._forecast_models for o in self._source.list_outputs())
        wait = min(cfg.interval, timedelta(hours=1)) if uncalibrated else cfg.interval
        if now - self._last_calibrated_at < wait:
            return
        self._recompute_models(now, exclude_ranges=self._active_incident_ranges(now))
        self._last_calibrated_at = now
        # Rétention (Perf-5) : réutilise ce même rythme périodique plutôt que
        # d'ajouter un second ordonnanceur pour une seule tâche de ménage.
        self._storage.purge_older_than(now - self._config.history.preferred)

    def _under_maintenance(self, output_id: str, at: datetime) -> bool:
        return any(w.covers(output_id, at) for w in self._maintenance)

    def _active_incident_ranges(self, now: datetime | None = None) -> dict[str, list[tuple[datetime, datetime]]]:
        """Plages à exclure de la recalibration : incidents en cours et maintenances (un comportement
        anormal ou volontairement perturbé ne doit pas devenir le « normal » appris)."""
        ranges: dict[str, list[tuple[datetime, datetime]]] = {}
        if now is not None:
            window_start = now - self._config.history.preferred
            for w in self._storage.list_maintenance(window_start, now):
                for output_id in [w.output_id] if w.output_id else self._source.list_outputs():
                    ranges.setdefault(output_id, []).append((w.start, w.end))
        for incident in self._incident_manager.active_incidents():
            start = incident.opened_at or incident.suspected_at
            if start is None:
                continue
            ranges.setdefault(incident.output_id, []).append((start, incident.last_seen_at))
        return ranges

    def _recompute_models(
        self, now: datetime, exclude_ranges: dict[str, list[tuple[datetime, datetime]]] | None
    ) -> None:
        """Calibre (ou recalibre) chaque sortie. L'historique est chargé **une sortie à la
        fois** et libéré avant la suivante : la mémoire de pointe ne dépend plus du nombre
        de sorties (elle suivait leur nombre quand tout l'historique était chargé d'un coup)."""
        start = now - self._config.history.preferred
        for output_id in self._source.list_outputs():
            history = self._source.load_history(start, now, output_id)
            if exclude_ranges:
                skip = exclude_ranges.get(output_id, [])
                history = [s for s in history if not any(a <= s.timestamp <= b for a, b in skip)]
            if not history:
                logger.warning("Historique vide pour %s : calibration ignorée", output_id)
                continue
            span = max(s.timestamp for s in history) - min(s.timestamp for s in history)
            if span < self._config.history.minimum:
                logger.warning(
                    "Historique insuffisant pour %s (%s < history.minimum=%s) : calibration ignorée",
                    output_id, span, self._config.history.minimum,
                )
                continue
            try:
                self._calibrate_output(output_id, history, initial=exclude_ranges is None)
            except Exception:
                logger.exception("Échec de calibration pour %s, modèle précédent conservé", output_id)

    def _calibrate_output(self, output_id: str, history: list[TrafficSample], initial: bool) -> None:
        interval = self._source.get_interval()
        self._forecast_models[output_id] = select_model(
            output_id,
            history,
            self._config.model_selection.minimum_history,
            self._config.model_selection.validation_window,
            allowed_models=self._config.forecast.models,
        )
        self._loss_baselines[output_id] = LossBaseline.from_history(history, self._config.detection.loss.loss_std_floor)
        forest_cfg = self._config.detection.isolation_forest
        if forest_cfg.enabled:
            forest = IsolationForestDetector(output_id, interval, forest_cfg.n_estimators)
            forest.fit(history)
            self._forests[output_id] = forest
        if initial:  # calibration initiale : amorce le tampon glissant des features
            tail = sorted(history, key=lambda x: x.timestamp)[-history_steps(interval) :]
            self._recent[output_id].extend(tail)

    def run_tick(self, now: datetime) -> None:
        """Un pas de la boucle (§30, étapes 1 à 11)."""
        # Fenêtres de maintenance : relues à chaque pas (quelques lignes) pour qu'une fenêtre créée ou
        # annulée par l'API s'applique au tick suivant, sans redémarrage.
        self._maintenance = self._storage.list_maintenance(now - timedelta(days=1), now + timedelta(days=1))
        # 1. récupérer les nouvelles données
        raw = [RawMeasurement(**s.model_dump()) for s in self._source.get_latest()]

        # 2. valider
        validated = self._validator.validate_batch(raw, now=now)
        for output_id in self._source.list_outputs():
            validated += self._validator.missing_slots(output_id, now)

        tick = _TickResults()
        tick.samples = [v.sample for v in validated if v.is_usable and v.sample is not None]
        for v in validated:
            self._process_validated_sample(v, tick)

        # 11. persister tout le tick en une transaction. Un incident jamais
        # confirmé (jamais passé par OPEN) n'est pas écrit : ce n'est que du
        # bruit résorbé seul (§19), voir `incident_worth_persisting`. Un
        # incident tout juste fermé, lui, l'est (il ne réapparaîtrait plus
        # dans active_incidents(), §29).
        alerts = [*tick.alerts, *self._escalations(now, tick)] if self._config.alerts.enabled else []
        self._storage.save_tick(
            tick.samples,
            tick.forecasts,
            tick.anomalies,
            [i for i in tick.incidents.values() if incident_worth_persisting(i)],
            alerts,
            # L'envoi est planifié dans la même transaction que l'alerte (outbox) : voir app/notify.py.
            self._notifier.plan(alerts) if self._notifier else [],
        )
        for sample in tick.samples:
            self.last_sample_at[sample.output_id] = max(sample.timestamp, self.last_sample_at.get(sample.output_id, sample.timestamp))
        self.maybe_recalibrate(now)

    def _escalations(self, now: datetime, tick: _TickResults) -> list[Alert]:
        """Un incident confirmé et en cours que personne n'a pris en charge depuis `notifications.escalate_after`
        émet UNE alerte `escalated` (l'escalade déjà émise se lit dans les alertes : pas de seconde). Jamais pour un incident
        en maintenance, déjà expliqué par une cause amont, acquitté, ni qui se ferme à ce pas, ni tant qu'il est en
        retour à la normale (la panne est déjà finie : on ne réveille pas le second groupe pour rien ; s'il rechute sans
        prise en charge, l'escalade repart)."""
        after = self._config.notifications.escalate_after
        if after is None:
            return []
        escalations = []
        for inc in self._storage.list_active_incidents():
            closing = tick.incidents.get(inc.id)
            if (
                inc.opened_at is None or inc.acknowledged_at is not None or inc.escalated_at is not None
                or inc.maintenance or inc.inhibited or now - inc.opened_at < after
                or self._incident_manager.is_recovering(inc.output_id)
                or (closing is not None and closing.status == IncidentStatus.CLOSED)
            ):
                continue
            started = inc.started_at or inc.opened_at
            minutes = round((now - inc.opened_at).total_seconds() / 60)
            escalations.append(Alert(
                incident_id=inc.id, output_id=inc.output_id, severity=inc.severity, event="escalated", timestamp=now,
                message=f"Aucun accusé de réception depuis {minutes} min : {inc.incident_type.value} sur {inc.output_id}",
                incident_type=inc.incident_type, duration_seconds=(now - started).total_seconds(), started_at=started,
            ))
        return escalations

    def _features(self, sample: TrafficSample, forecast: ForecastResult, baseline: LossBaseline) -> dict[str, float]:
        recent = self._recent[sample.output_id]
        features = compute_features(sample, forecast.predicted_traffic_mbps, baseline.median_loss_percent, recent)
        if self._config.detection.extended_features.enabled:
            features |= compute_extended_features(sample, recent, self._source.get_interval()) or {}
        return features

    def _process_validated_sample(self, validated, tick: _TickResults) -> None:
        output_id = validated.output_id
        model = self._forecast_models.get(output_id)
        baseline = self._loss_baselines.get(output_id)
        if model is None or baseline is None:
            return  # sortie non calibrée : rien à comparer

        if not validated.is_usable:
            # 5-7. pas de comparaison possible : anomalie neutre marquant le trou (§31).
            gap_result = AnomalyResult(
                output_id=output_id,
                timestamp=validated.timestamp,
                traffic_anomaly_score=0.0,
                loss_anomaly_score=0.0,
                joint_anomaly_score=0.0,
                data_gap=True,
            )
            alerts, incident = self._incident_manager.process(gap_result)
            self._collect(tick, alerts, incident)
            return

        sample = validated.sample
        interval = self._source.get_interval()

        # 4. produire le forecast (un seul pas, à l'instant de l'observation)
        forecast = model.predict(validated.timestamp - interval, interval, interval)[0]
        tick.forecasts.append(forecast)

        # 5-7. comparer réel/prévision, analyser les pertes, calculer les scores
        anomaly = build_anomaly_result(
            sample, forecast, baseline, self._persistence, self._config.incident.open_score,
            loss_enabled=self._config.detection.loss.enabled,
        )
        # Features (§9) et score Isolation Forest (§17) : enrichissement, sans
        # effet sur les scores ci-dessus ni sur la décision d'ouvrir un incident.
        anomaly.features = self._features(sample, forecast, baseline)
        forest = self._forests.get(output_id)
        if forest is not None:
            anomaly.isolation_score = forest.score(sample, self._recent[output_id])
        self._recent[output_id].append(sample)
        tick.anomalies.append(anomaly)

        # 8-9. corrélation + mise à jour des incidents (gérées par IncidentManager)
        alerts, incident = self._incident_manager.process(
            anomaly,
            expected_traffic_mbps=forecast.predicted_traffic_mbps,
            observed_traffic_mbps=sample.traffic_mbps,
            observed_loss_percent=sample.loss_percent,
        )
        self._collect(tick, alerts, incident)

    @staticmethod
    def _collect(tick: _TickResults, alerts: list[Alert], incident: Incident | None) -> None:
        tick.alerts.extend(alerts)
        if incident is not None:
            tick.incidents[incident.id] = incident

    def run_forever(self, start_at: datetime, max_ticks: int | None = None) -> None:
        """Boucle continue. `max_ticks` sert essentiellement aux tests/démonstrations."""
        if hasattr(self._source, "start_realtime"):
            self._source.start_realtime(start_at)
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            now = start_at + (ticks + 1) * self._source.get_interval()
            self.run_tick(now)
            ticks += 1
