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
from network_monitor.app.models import AnomalyResult, ForecastResult, RawMeasurement, TrafficSample
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
            config.incident, critical_score=config.alerts.critical_score
        )
        self._persistence = PersistenceTracker(interval_seconds=source.get_interval().total_seconds())
        self._forecast_models: dict[str, ForecastModel] = {}
        self._loss_baselines: dict[str, LossBaseline] = {}

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
        start = now - self._config.history.preferred
        for output_id in self._source.list_outputs():
            history = self._source.load_history(start, now)
            history = [s for s in history if s.output_id == output_id]
            if len(history) < 1:
                logger.warning("Historique vide pour %s : calibration ignorée", output_id)
                continue

            self._forecast_models[output_id] = select_model(
                output_id,
                history,
                self._config.model_selection.minimum_history,
                self._config.model_selection.validation_window,
            )
            self._loss_baselines[output_id] = LossBaseline.from_history(
                history, self._config.detection.loss.loss_std_floor
            )

    def run_tick(self, now: datetime) -> None:
        """Un pas de la boucle (§30, étapes 1 à 11)."""
        # 1. récupérer les nouvelles données
        raw = [RawMeasurement(**s.model_dump()) for s in self._source.get_latest()]

        # 2. valider
        validated = self._validator.validate_batch(raw, now=now)
        for output_id in self._source.list_outputs():
            validated += self._validator.missing_slots(output_id, now)

        valid_samples: list[TrafficSample] = [v.sample for v in validated if v.is_usable]
        if valid_samples:
            self._storage.save_samples(valid_samples)

        for v in validated:
            self._process_validated_sample(v, now)

    def _process_validated_sample(self, validated, now: datetime) -> None:
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
            _, incident = self._incident_manager.process(gap_result)
            if incident is not None:
                self._storage.upsert_incident(incident)
            return

        sample = validated.sample

        # 4. produire le forecast (un seul pas, à l'instant de l'observation)
        forecasts = model.predict(
            validated.timestamp - self._source.get_interval(),
            self._source.get_interval(),
            self._source.get_interval(),
        )
        forecast = forecasts[0]
        self._storage.save_forecasts([forecast])

        # 5-7. comparer réel/prévision, analyser les pertes, calculer les scores
        anomaly = build_anomaly_result(
            sample, forecast, baseline, self._persistence, self._config.incident.open_score
        )
        self._storage.save_anomalies([anomaly])

        # 8-9. corrélation + mise à jour des incidents (gérées par IncidentManager)
        _, incident = self._incident_manager.process(
            anomaly,
            expected_traffic_mbps=forecast.predicted_traffic_mbps,
            observed_traffic_mbps=sample.traffic_mbps,
            observed_loss_percent=sample.loss_percent,
        )

        # 11. persister le résultat : y compris un incident tout juste fermé,
        # qui ne réapparaîtrait plus jamais dans active_incidents() (§29).
        if incident is not None:
            self._storage.upsert_incident(incident)

    def run_forever(self, start_at: datetime, max_ticks: int | None = None) -> None:
        """Boucle continue. `max_ticks` sert essentiellement aux tests/démonstrations."""
        if hasattr(self._source, "start_realtime"):
            self._source.start_realtime(start_at)
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            now = start_at + (ticks + 1) * self._source.get_interval()
            self.run_tick(now)
            ticks += 1
