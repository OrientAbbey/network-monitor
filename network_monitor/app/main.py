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
    RawMeasurement,
    TrafficSample,
)
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

    def __init__(self, source: DataSource, storage: Repository, config: AppConfig) -> None:
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
        self._persistence = PersistenceTracker(interval_seconds=source.get_interval().total_seconds())
        self._forecast_models: dict[str, ForecastModel] = {}
        self._loss_baselines: dict[str, LossBaseline] = {}
        self._forests: dict[str, IsolationForestDetector] = {}
        # Mesures précédentes de chaque sortie (1 h), pour les features glissantes (§9).
        self._recent: dict[str, deque[TrafficSample]] = {
            o: deque(maxlen=history_steps(source.get_interval())) for o in source.list_outputs()
        }
        self._last_calibrated_at: datetime | None = None

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
        if now - self._last_calibrated_at < cfg.interval:
            return
        self._recompute_models(now, exclude_ranges=self._active_incident_ranges())
        self._last_calibrated_at = now
        # Rétention (Perf-5) : réutilise ce même rythme périodique plutôt que
        # d'ajouter un second ordonnanceur pour une seule tâche de ménage.
        self._storage.purge_older_than(now - self._config.history.preferred)

    def _active_incident_ranges(self) -> dict[str, list[tuple[datetime, datetime]]]:
        ranges: dict[str, list[tuple[datetime, datetime]]] = {}
        for incident in self._incident_manager.active_incidents():
            start = incident.opened_at or incident.suspected_at
            if start is None:
                continue
            ranges.setdefault(incident.output_id, []).append((start, incident.last_seen_at))
        return ranges

    def _recompute_models(
        self, now: datetime, exclude_ranges: dict[str, list[tuple[datetime, datetime]]] | None
    ) -> None:
        start = now - self._config.history.preferred
        # Un seul chargement pour toutes les sorties (au lieu d'un par sortie,
        # qui rechargeait l'historique complet de la source N fois).
        all_history = self._source.load_history(start, now)
        by_output: dict[str, list[TrafficSample]] = {}
        for sample in all_history:
            if exclude_ranges and any(
                a <= sample.timestamp <= b for a, b in exclude_ranges.get(sample.output_id, [])
            ):
                continue
            by_output.setdefault(sample.output_id, []).append(sample)

        for output_id in self._source.list_outputs():
            history = by_output.get(output_id, [])
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
                self._forecast_models[output_id] = select_model(
                    output_id,
                    history,
                    self._config.model_selection.minimum_history,
                    self._config.model_selection.validation_window,
                    allowed_models=self._config.forecast.models,
                )
                self._loss_baselines[output_id] = LossBaseline.from_history(
                    history, self._config.detection.loss.loss_std_floor
                )
                if self._config.detection.isolation_forest.enabled:
                    forest = IsolationForestDetector(
                        output_id, self._source.get_interval(), self._config.detection.isolation_forest.n_estimators
                    )
                    forest.fit(history)
                    self._forests[output_id] = forest
                if exclude_ranges is None:  # calibration initiale : amorce le tampon glissant
                    tail = sorted(history, key=lambda x: x.timestamp)[-history_steps(self._source.get_interval()) :]
                    self._recent[output_id].extend(tail)
            except Exception:  # pragma: no cover - défensif : ne jamais interrompre le monitoring
                logger.exception("Échec de calibration pour %s, modèle précédent conservé", output_id)

    def run_tick(self, now: datetime) -> None:
        """Un pas de la boucle (§30, étapes 1 à 11)."""
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
        self._storage.save_tick(
            tick.samples,
            tick.forecasts,
            tick.anomalies,
            [i for i in tick.incidents.values() if incident_worth_persisting(i)],
            tick.alerts if self._config.alerts.enabled else [],
        )
        self.maybe_recalibrate(now)

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
