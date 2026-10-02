"""
Boucle temps réel (§30) : assemble les modules sans logique métier propre.
Chaque étape correspond explicitement à une étape du §30 pour rester lisible.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from network_monitor.analysis.detection import LossBaseline, PersistenceTracker, build_anomaly_result
from network_monitor.analysis.forecasting import ForecastModel, select_model
from network_monitor.app.config import AppConfig
from network_monitor.app.models import (
    Alert,
    AnomalyResult,
    DataQualityFlag,
    ForecastResult,
    Incident,
    RawMeasurement,
    TrafficSample,
)
from network_monitor.data.source import DataSource, DataValidator
from network_monitor.incidents.manager import IncidentManager
from network_monitor.storage.repository import Repository

logger = logging.getLogger(__name__)


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
        self._incident_manager = IncidentManager(
            config.incident,
            critical_score=config.alerts.critical_score,
            alerts_enabled=config.alerts.enabled,
        )
        self._persistence = PersistenceTracker(interval_seconds=source.get_interval().total_seconds())
        self._forecast_models: dict[str, ForecastModel] = {}
        self._loss_baselines: dict[str, LossBaseline] = {}
        self._last_calibrated_at: datetime | None = None
        self._last_purged_at: datetime | None = None

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

    def calibrate(
        self,
        now: datetime,
        exclude_windows: list[tuple[datetime, datetime]] | None = None,
    ) -> None:
        """
        Calibration (§8) : un modèle et une baseline de pertes par sortie.

        `exclude_windows` retire de l'historique des périodes anormales (recalibration, §32).
        Si plus de la moitié de l'historique d'une sortie disparaît, son modèle actuel est conservé.
        """
        start = now - self._config.history.preferred
        full_history = self._source.load_history(start, now)
        by_output: dict[str, list[TrafficSample]] = {}
        for s in full_history:
            by_output.setdefault(s.output_id, []).append(s)

        for output_id in self._source.list_outputs():
            history = by_output.get(output_id, [])
            if not history:
                logger.warning("Historique vide pour %s : calibration ignorée", output_id)
                continue

            if exclude_windows:
                clean = [s for s in history if not _in_any_window(s.timestamp, exclude_windows)]
                if len(clean) < 0.5 * len(history):
                    logger.warning(
                        "Recalibration de %s ignorée : %d%% de l'historique est couvert par des incidents",
                        output_id,
                        round(100 * (1 - len(clean) / len(history))),
                    )
                    continue
                history = clean

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
        if self._last_calibrated_at is None:
            self._restore_incidents()  # premier démarrage seulement : une recalibration ne doit pas toucher à l'état
        self._last_calibrated_at = now

    def _restore_incidents(self) -> None:
        active = self._storage.list_active_incidents()
        if not active:
            return
        superseded = self._incident_manager.restore(active)
        if superseded:
            logger.warning(
                "%d incident(s) actif(s) orphelin(s) fermé(s) : un seul incident actif est suivi par sortie",
                len(superseded),
            )
            self._storage.save_tick([], [], [], superseded)
        logger.info("%d incident(s) actif(s) repris après redémarrage", len(self._incident_manager.active_incidents()))

    def _abnormal_windows(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Périodes couvertes par un incident CONFIRMÉ (ouvert au moins une fois), à exclure de la recalibration (§32)."""
        windows = []
        for incident in self._storage.list_incidents_overlapping(start, end):
            if incident.opened_at is None:
                continue  # simple signal faible jamais confirmé : pas une période anormale
            first = incident.suspected_at or incident.opened_at
            last = incident.closed_at or incident.last_seen_at
            windows.append((first, last))
        return windows

    def _maybe_recalibrate(self, now: datetime) -> None:
        interval = self._config.monitoring.recalibration_interval
        if interval is None or self._last_calibrated_at is None:
            return
        if now - self._last_calibrated_at < interval:
            return
        try:
            windows = self._abnormal_windows(now - self._config.history.preferred, now)
            self.calibrate(now, exclude_windows=windows)
            logger.info("Recalibration effectuée à %s (%d période(s) anormale(s) exclue(s))", now, len(windows))
        except Exception:
            # On garde les modèles actuels plutôt que d'arrêter la supervision ; nouvel essai à l'échéance suivante.
            logger.exception("Recalibration échouée à %s : modèles précédents conservés", now)
            self._last_calibrated_at = now

    def _maybe_purge(self, now: datetime) -> None:
        retention = self._config.storage.retention
        if retention is None:
            return
        if self._last_purged_at is not None and now - self._last_purged_at < self._config.storage.purge_interval:
            return
        self._last_purged_at = now
        deleted = self._storage.purge_before(now - retention)
        if any(deleted.values()):
            logger.info("Purge de rétention (%s) : %s", retention, deleted)

    def run_tick(self, now: datetime) -> None:
        """Un pas de la boucle (§30, étapes 1 à 11)."""
        # 1. récupérer les nouvelles données
        raw = [RawMeasurement(**s.model_dump()) for s in self._source.get_latest()]

        # 2. valider. Les trous sont cherchés AVANT validate_batch, qui avance le dernier timestamp
        # valide et masquerait un trou de plusieurs pas. Borne `now - 2 * interval` : l'échantillon
        # de ce tick est daté `now - interval`, il ne doit pas être compté comme manquant.
        interval = self._source.get_interval()
        missing: list = []
        for output_id in self._source.list_outputs():
            missing += self._validator.missing_slots(output_id, now - 2 * interval)
        validated = missing + self._validator.validate_batch(raw, now=now)

        valid_samples: list[TrafficSample] = [v.sample for v in validated if v.is_usable]

        # 3-11. Un seul commit par tick : moins de synchronisations SQLite, et le tick est atomique.
        tick_forecasts: list[ForecastResult] = []
        tick_anomalies: list[AnomalyResult] = []
        tick_incidents: list[Incident] = []
        tick_alerts: list[Alert] = []
        for v in validated:
            forecast, anomaly, incident, alerts = self._process_validated_sample(v)
            tick_alerts.extend(alerts)
            if forecast is not None:
                tick_forecasts.append(forecast)
            if anomaly is not None:
                tick_anomalies.append(anomaly)
            if incident is not None:
                tick_incidents.append(incident)

        self._storage.save_tick(valid_samples, tick_forecasts, tick_anomalies, tick_incidents, tick_alerts)

        # Après la persistance : la recalibration lit les incidents qui viennent d'être enregistrés (§32).
        self._maybe_recalibrate(now)
        self._maybe_purge(now)

    def _process_validated_sample(
        self, validated
    ) -> tuple[ForecastResult | None, AnomalyResult | None, Incident | None, list[Alert]]:
        """Renvoie ce qui doit être persisté pour cette mesure (l'écriture est groupée dans `run_tick`)."""
        output_id = validated.output_id
        model = self._forecast_models.get(output_id)
        baseline = self._loss_baselines.get(output_id)
        if model is None or baseline is None:
            return None, None, None, []  # sortie non calibrée : rien à comparer

        if not validated.is_usable and DataQualityFlag.DUPLICATE in validated.quality_flags:
            # Doublon : déjà traité. Ce n'est pas une absence de donnée (ce serait une fausse preuve `data_gap`).
            logger.debug("Doublon ignoré pour %s à %s", output_id, validated.timestamp)
            return None, None, None, []

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
            return None, None, incident, alerts

        sample = validated.sample

        # 4. produire le forecast (un seul pas, à l'instant de l'observation)
        forecasts = model.predict(
            validated.timestamp - self._source.get_interval(),
            self._source.get_interval(),
            self._source.get_interval(),
        )
        forecast = forecasts[0]

        # 5-7. comparer réel/prévision, analyser les pertes, calculer les scores
        anomaly = build_anomaly_result(
            sample,
            forecast,
            baseline,
            self._persistence,
            self._config.incident.open_score,
            traffic_enabled=self._config.detection.traffic.enabled,
            loss_enabled=self._config.detection.loss.enabled,
        )

        # 8-9. corrélation + mise à jour des incidents (gérées par IncidentManager)
        alerts, incident = self._incident_manager.process(
            anomaly,
            expected_traffic_mbps=forecast.predicted_traffic_mbps,
            observed_traffic_mbps=sample.traffic_mbps,
            observed_loss_percent=sample.loss_percent,
        )

        # 11. Un incident tout juste fermé ne réapparaîtra plus dans active_incidents() (§29) :
        # il doit être renvoyé pour être persisté.
        return forecast, anomaly, incident, alerts

    def run_forever(self, start_at: datetime, max_ticks: int | None = None) -> None:
        """Boucle continue. `max_ticks` sert essentiellement aux tests/démonstrations."""
        if hasattr(self._source, "start_realtime"):
            self._source.start_realtime(start_at)
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            now = start_at + (ticks + 1) * self._source.get_interval()
            self.run_tick(now)
            ticks += 1


def _in_any_window(ts: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    return any(start <= ts <= end for start, end in windows)
