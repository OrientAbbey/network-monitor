"""
Interface DataSource et validation des données.

Le moteur d'analyse ne doit jamais dépendre directement de MockDataSource :
il dépend uniquement du contrat abstrait `DataSource`. Toute source réelle
future n'a qu'à convertir ses propres données vers `RawMeasurement` /
`TrafficSample` pour être compatible.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from network_monitor.app.models import (
    DataQualityFlag,
    RawMeasurement,
    TrafficSample,
    ValidatedSample,
)


class DataSource(ABC):
    """Contrat commun à toute source de données, Mock ou réelle."""

    @abstractmethod
    def load_history(self, start: datetime, end: datetime, output_id: str | None = None) -> list[TrafficSample]:
        """Retourne l'historique normalisé entre start (inclus) et end (exclu).

        `output_id` restreint le résultat à une sortie : la calibration charge
        ainsi une sortie à la fois, au lieu de garder l'historique de toutes
        les sorties en mémoire en même temps.
        """
        raise NotImplementedError

    @abstractmethod
    def get_latest(self) -> list[TrafficSample]:
        """Retourne les nouvelles mesures normalisées depuis le dernier appel."""
        raise NotImplementedError

    @abstractmethod
    def get_interval(self) -> timedelta:
        """Fréquence native de la source."""
        raise NotImplementedError

    @abstractmethod
    def list_outputs(self) -> list[str]:
        """Sorties connues de la source, nécessaire pour détecter une sortie inconnue."""
        raise NotImplementedError


class DataValidator:
    """
    Étape "Data Validation" du pipeline.

    Détecte, sans jamais planter sur une valeur invalide : sortie inconnue,
    doublon, valeur négative, perte hors bornes, données arrivant en retard.
    Les trous temporels (données totalement absentes) sont traités
    séparément par `missing_slots`, car ils ne portent sur aucune mesure
    reçue mais sur une absence.
    """

    def __init__(
        self,
        known_outputs: list[str],
        expected_interval: timedelta,
        late_arrival_tolerance: timedelta | None = None,
    ) -> None:
        self._known_outputs = set(known_outputs)
        self._expected_interval = expected_interval
        self._late_arrival_tolerance = late_arrival_tolerance or expected_interval
        self._seen_keys: set[tuple[str, datetime]] = set()
        self._last_valid_timestamp: dict[str, datetime] = {}
        self._missing_emitted_until: dict[str, datetime] = {}
        """Dernier créneau déjà signalé MISSING par `missing_slots`, pour ne
        jamais réémettre deux fois le même trou (idempotence)."""

    def validate_batch(
        self, raw_measurements: list[RawMeasurement], now: datetime | None = None
    ) -> list[ValidatedSample]:
        """
        Valide un lot de mesures brutes reçues à l'instant `now`
        (par défaut : le timestamp le plus récent du lot).
        """
        if now is None and raw_measurements:
            now = max(m.timestamp for m in raw_measurements)

        results: list[ValidatedSample] = []
        for raw in raw_measurements:
            flags = self._flags_for(raw, now)
            blocking = {
                DataQualityFlag.NEGATIVE_TRAFFIC,
                DataQualityFlag.LOSS_OUT_OF_RANGE,
                DataQualityFlag.UNKNOWN_OUTPUT,
                DataQualityFlag.DUPLICATE,
            }
            sample: TrafficSample | None = None
            if not blocking.intersection(flags):
                sample = TrafficSample(
                    timestamp=raw.timestamp,
                    output_id=raw.output_id,
                    traffic_mbps=raw.traffic_mbps,
                    loss_percent=raw.loss_percent,
                )
                prev = self._last_valid_timestamp.get(raw.output_id)
                if prev is None or raw.timestamp > prev:
                    self._last_valid_timestamp[raw.output_id] = raw.timestamp

            results.append(
                ValidatedSample(
                    output_id=raw.output_id,
                    timestamp=raw.timestamp,
                    sample=sample,
                    quality_flags=flags,
                )
            )
        return results

    def _flags_for(self, raw: RawMeasurement, now: datetime | None) -> list[DataQualityFlag]:
        flags: list[DataQualityFlag] = []

        if raw.output_id not in self._known_outputs:
            flags.append(DataQualityFlag.UNKNOWN_OUTPUT)

        key = (raw.output_id, raw.timestamp)
        if key in self._seen_keys:
            flags.append(DataQualityFlag.DUPLICATE)
        else:
            self._seen_keys.add(key)

        if raw.traffic_mbps < 0:
            flags.append(DataQualityFlag.NEGATIVE_TRAFFIC)

        if not (0.0 <= raw.loss_percent <= 100.0):
            flags.append(DataQualityFlag.LOSS_OUT_OF_RANGE)

        if now is not None and (now - raw.timestamp) > self._late_arrival_tolerance:
            flags.append(DataQualityFlag.LATE_ARRIVAL)

        return flags

    def missing_slots(self, output_id: str, up_to: datetime) -> list[ValidatedSample]:
        """
        Génère un ValidatedSample(sample=None, MISSING) pour chaque
        timestamp attendu (au pas `expected_interval`) entre le dernier
        timestamp valide connu pour cette sortie (exclu) et `up_to` (inclus).

        Idempotent : un créneau déjà signalé MISSING lors d'un appel
        précédent n'est jamais réémis, même si `up_to` recouvre une plage
        déjà couverte (appelé à chaque tick avec un `up_to` croissant).

        Retourne une liste vide si la sortie n'a encore jamais été vue
        (pas de référence temporelle pour détecter un trou).
        """
        last = self._last_valid_timestamp.get(output_id)
        if last is None:
            return []

        cursor = max(last, self._missing_emitted_until.get(output_id, last))
        missing: list[ValidatedSample] = []
        slot_time = cursor + self._expected_interval
        while slot_time <= up_to:
            missing.append(
                ValidatedSample(
                    output_id=output_id,
                    timestamp=slot_time,
                    sample=None,
                    quality_flags=[DataQualityFlag.MISSING],
                )
            )
            self._missing_emitted_until[output_id] = slot_time
            slot_time += self._expected_interval
        return missing

    def known_outputs(self) -> set[str]:
        return set(self._known_outputs)

    def register_output(self, output_id: str) -> None:
        """Permet d'enregistrer une sortie découverte dynamiquement (usage avancé)."""
        self._known_outputs.add(output_id)
