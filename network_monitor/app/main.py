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
        #
        # Les trous DOIVENT être détectés avant que validate_batch() ne mette à
        # jour _last_valid_timestamp avec l'échantillon du tick courant, sinon
        # un vrai trou multi-pas serait masqué par la reprise (le dernier
        # timestamp valide sauterait directement au point de reprise sans
        # jamais laisser missing_slots voir les pas manqués entre les deux).
        #
        # La borne `now - 2 * interval` (et non `now`) reflète le décalage
        # structurel d'un pas entre l'horloge simulée `now` et le timestamp du
        # dernier échantillon réellement disponible (`now - interval`, cf.
        # MockDataSource.get_latest) : sans ce décalage, missing_slots signale
        # à chaque tick un créneau qui n'est simplement pas encore dû, pas
        # réellement manquant (cf. PLAN_CORRECTION.md §3.1).
        interval = self._source.get_interval()
        missing: list = []
        for output_id in self._source.list_outputs():
            missing += self._validator.missing_slots(output_id, now - 2 * interval)
        validated = missing + self._validator.validate_batch(raw, now=now)

        valid_samples: list[TrafficSample] = [v.sample for v in validated if v.is_usable]

        # 3-11. Un seul commit pour tout le tick plutôt qu'un commit par sortie
        # et par type de résultat (jusqu'à 10 auparavant) : réduit d'autant
        # les points de synchronisation SQLite et rend le tick atomique
        # (REVUE.md, Perf-1 — complément à WAL, cf. storage/repository.py).
        tick_forecasts: list[ForecastResult] = []
        tick_anomalies: list[AnomalyResult] = []
        tick_incidents: list[Incident] = []
        for v in validated:
            forecast, anomaly, incident = self._process_validated_sample(v, now)
            if forecast is not None:
                tick_forecasts.append(forecast)
            if anomaly is not None:
                tick_anomalies.append(anomaly)
            if incident is not None:
                tick_incidents.append(incident)

        self._storage.save_tick(valid_samples, tick_forecasts, tick_anomalies, tick_incidents)

    def _process_validated_sample(
        self, validated, now: datetime
    ) -> tuple[ForecastResult | None, AnomalyResult | None, Incident | None]:
        """
        Traite une mesure validée et renvoie ce qui doit être persisté pour
        ce point, sans rien écrire lui-même : `run_tick` regroupe l'écriture
        de tout le tick en une seule transaction (voir plus haut).
        """
        output_id = validated.output_id
        model = self._forecast_models.get(output_id)
        baseline = self._loss_baselines.get(output_id)
        if model is None or baseline is None:
            return None, None, None  # sortie non calibrée : rien à comparer

        if not validated.is_usable and DataQualityFlag.DUPLICATE in validated.quality_flags:
            # Doublon : la mesure de ce créneau a déjà été traitée. Ce n'est PAS
            # une absence de donnée — le traiter comme un trou ajouterait une
            # fausse preuve `data_gap` à un incident actif (PLAN_CORRECTION.md §3.4).
            logger.debug("Doublon ignoré pour %s à %s", output_id, validated.timestamp)
            return None, None, None

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
            return None, None, incident

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
            sample, forecast, baseline, self._persistence, self._config.incident.open_score
        )

        # 8-9. corrélation + mise à jour des incidents (gérées par IncidentManager)
        _, incident = self._incident_manager.process(
            anomaly,
            expected_traffic_mbps=forecast.predicted_traffic_mbps,
            observed_traffic_mbps=sample.traffic_mbps,
            observed_loss_percent=sample.loss_percent,
        )

        # 11. le résultat (y compris un incident tout juste fermé, qui ne
        # réapparaîtrait plus jamais dans active_incidents(), §29) est
        # renvoyé à l'appelant plutôt que persisté ici.
        return forecast, anomaly, incident

    def run_forever(self, start_at: datetime, max_ticks: int | None = None) -> None:
        """Boucle continue. `max_ticks` sert essentiellement aux tests/démonstrations."""
        if hasattr(self._source, "start_realtime"):
            self._source.start_realtime(start_at)
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            now = start_at + (ticks + 1) * self._source.get_interval()
            self.run_tick(now)
            ticks += 1
