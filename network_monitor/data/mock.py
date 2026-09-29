"""
Implémentation Mock de DataSource (§7).

Sert à la fois de source de données pour développer/tester le système sans
source réelle, et d'environnement de validation (§33) grâce à sa vérité
terrain interne, jamais transmise au détecteur.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from enum import Enum

import numpy as np
from pydantic import BaseModel, Field

from network_monitor.app.models import Severity, TrafficSample
from network_monitor.data.source import DataSource

# ---------------------------------------------------------------------------
# Profils de sortie
# ---------------------------------------------------------------------------


class OutputProfile(BaseModel):
    """
    Comportement "normal" d'une sortie réseau (§7 : saisonnalité quotidienne
    et hebdomadaire, bruit, heures de pointe/creuses, échelle propre).
    """

    output_id: str
    base_level_mbps: float = Field(gt=0)

    # Amplitude relative de la variation quotidienne autour du niveau de base
    # (0.3 => le trafic varie de ±30% environ entre heures creuses et de pointe).
    daily_amplitude: float = 0.35
    # Décalage en heures du pic quotidien (0 = pic à minuit, 14 = pic à 14h).
    daily_peak_hour: float = 14.0

    # Multiplicateur appliqué le week-end (< 1 pour un trafic plus faible,
    # typique d'un réseau à dominante professionnelle).
    weekend_factor: float = 0.55

    # Écart-type du bruit multiplicatif (log-normal) appliqué au trafic.
    traffic_noise_std: float = 0.06

    # Niveau et dispersion habituels des pertes. Certaines sorties ont une
    # dispersion volontairement quasi nulle pour exercer le plancher
    # loss_std_floor (§15) côté détection.
    base_loss_percent: float = 0.15
    loss_noise_std: float = 0.05


DEFAULT_PROFILES: list[OutputProfile] = [
    OutputProfile(
        output_id="OUT_001",
        base_level_mbps=300.0,  # quelques centaines de Mbps
        daily_amplitude=0.40,
        weekend_factor=0.5,
        loss_noise_std=0.06,
    ),
    OutputProfile(
        output_id="OUT_002",
        base_level_mbps=3_000.0,  # quelques Gbps
        daily_amplitude=0.30,
        weekend_factor=0.65,
        loss_noise_std=0.03,
    ),
    OutputProfile(
        output_id="OUT_003",
        base_level_mbps=32_000.0,  # plusieurs dizaines de Gbps
        daily_amplitude=0.20,
        weekend_factor=0.75,
        base_loss_percent=0.05,
        loss_noise_std=0.015,  # dispersion quasi nulle : exerce loss_std_floor
    ),
]


# ---------------------------------------------------------------------------
# Scénarios d'anomalie
# ---------------------------------------------------------------------------


class ScenarioType(str, Enum):
    """Les dix scénarios d'anomalie requis par le §7."""

    TRAFFIC_DROP = "traffic_drop"
    TRAFFIC_SPIKE = "traffic_spike"
    HIGH_LOSS = "high_loss"
    TRAFFIC_DROP_HIGH_LOSS = "traffic_drop_high_loss"
    PROGRESSIVE_DEGRADATION = "progressive_degradation"
    INTERMITTENT_INCIDENT = "intermittent_incident"
    SHORT_INCIDENT = "short_incident"
    LONG_INCIDENT = "long_incident"
    PROGRESSIVE_RECOVERY = "progressive_recovery"
    COMBINED = "combined"


class AnomalyScenario(BaseModel):
    """
    Vérité terrain interne d'un scénario injecté (§7). Ce modèle n'est
    jamais transmis au détecteur : il sert uniquement à générer le signal
    et, plus tard, à évaluer les performances du système (§33).
    """

    scenario_id: str
    output_id: str
    start_time: datetime
    end_time: datetime
    type: ScenarioType
    severity: Severity

    # Paramètres numériques de l'effet (valeurs par défaut raisonnables,
    # ajustables par scénario).
    traffic_drop_fraction: float = 0.6
    traffic_spike_multiplier: float = 2.5
    loss_offset_points: float = 8.0
    intermittent_period: timedelta = timedelta(minutes=15)
    intermittent_duty_cycle: float = 0.5


def _scenario_effect(scenario: AnomalyScenario, elapsed: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Calcule, pour chaque instant du scénario (`elapsed` dans [0, 1],
    fraction écoulée entre start_time et end_time), le facteur
    multiplicatif à appliquer au trafic et le décalage additif à
    appliquer aux pertes (en points de %).

    Le mapping type -> forme d'effet est un choix d'implémentation du
    Mock, pas une spécification du cahier des charges au-delà de la
    liste des dix types attendus (§7).
    """
    e = np.clip(elapsed, 0.0, 1.0)
    t = scenario.type

    if t == ScenarioType.TRAFFIC_DROP:
        return np.full_like(e, 1.0 - scenario.traffic_drop_fraction), np.zeros_like(e)

    if t == ScenarioType.TRAFFIC_SPIKE:
        return np.full_like(e, scenario.traffic_spike_multiplier), np.zeros_like(e)

    if t == ScenarioType.HIGH_LOSS:
        return np.ones_like(e), np.full_like(e, scenario.loss_offset_points)

    if t in (ScenarioType.TRAFFIC_DROP_HIGH_LOSS, ScenarioType.SHORT_INCIDENT, ScenarioType.LONG_INCIDENT):
        # SHORT_INCIDENT / LONG_INCIDENT ont le même effet numérique que
        # TRAFFIC_DROP_HIGH_LOSS : ce qui les distingue est la durée de
        # l'instance (start_time -> end_time), pas la forme de l'effet.
        return (
            np.full_like(e, 1.0 - scenario.traffic_drop_fraction),
            np.full_like(e, scenario.loss_offset_points),
        )

    if t == ScenarioType.PROGRESSIVE_DEGRADATION:
        return 1.0 - scenario.traffic_drop_fraction * e, scenario.loss_offset_points * e

    if t == ScenarioType.PROGRESSIVE_RECOVERY:
        # Démarre totalement dégradé, revient linéairement à la normale.
        return (
            1.0 - scenario.traffic_drop_fraction * (1.0 - e),
            scenario.loss_offset_points * (1.0 - e),
        )

    if t == ScenarioType.INTERMITTENT_INCIDENT:
        total_duration = (scenario.end_time - scenario.start_time).total_seconds()
        period_fraction = scenario.intermittent_period.total_seconds() / total_duration
        phase = np.mod(e / period_fraction, 1.0)
        on = phase < scenario.intermittent_duty_cycle
        traffic_factor = np.where(on, 1.0 - scenario.traffic_drop_fraction, 1.0)
        loss_offset = np.where(on, scenario.loss_offset_points, 0.0)
        return traffic_factor, loss_offset

    if t == ScenarioType.COMBINED:
        # Choix d'implémentation : dégradation progressive du trafic
        # combinée à des pics de pertes intermittents, pour représenter
        # un scénario multi-anomalies réaliste (§7, point 10).
        traffic_factor = 1.0 - scenario.traffic_drop_fraction * e
        phase = np.mod(e * 4.0, 1.0)
        loss_offset = np.where(phase < 0.3, scenario.loss_offset_points, 0.0)
        return traffic_factor, loss_offset

    return np.ones_like(e), np.zeros_like(e)


# ---------------------------------------------------------------------------
# MockDataSource
# ---------------------------------------------------------------------------


class MockDataSource(DataSource):
    """
    Générateur Mock (§7). Fonctionne immédiatement sans source réelle,
    supporte génération historique (`load_history`) et continue
    (`get_latest`), et permet l'injection contrôlée de scénarios via
    `add_scenario`.
    """

    def __init__(
        self,
        profiles: list[OutputProfile] | None = None,
        interval: timedelta = timedelta(minutes=5),
        seed: int = 42,
    ) -> None:
        self._profiles: dict[str, OutputProfile] = {
            p.output_id: p for p in (profiles or DEFAULT_PROFILES)
        }
        self._interval = interval
        self._rng = np.random.default_rng(seed)
        self._scenarios: list[AnomalyScenario] = []
        # L'API ajoute des scénarios depuis un thread HTTP pendant que la
        # boucle de monitoring lit/génère depuis un autre thread : le verrou
        # (réentrant, get_latest() appelle _generate_range()) garantit que
        # chaque génération voit une liste de scénarios cohérente
        # (REVUE.md, Conc-3).
        self._lock = threading.RLock()
        self._clock: datetime | None = None

    # -- Contrat DataSource ------------------------------------------------

    def list_outputs(self) -> list[str]:
        return list(self._profiles.keys())

    def get_interval(self) -> timedelta:
        return self._interval

    def load_history(self, start: datetime, end: datetime) -> list[TrafficSample]:
        with self._lock:
            return self._generate_range(start, end)

    def start_realtime(self, now: datetime) -> None:
        """Initialise le curseur temps réel utilisé par `get_latest`."""
        with self._lock:
            self._clock = now

    def get_latest(self) -> list[TrafficSample]:
        with self._lock:
            if self._clock is None:
                raise RuntimeError(
                    "Curseur temps réel non initialisé : appeler start_realtime(now) au préalable."
                )
            window_start = self._clock
            window_end = self._clock + self._interval
            self._clock = window_end
            return self._generate_range(window_start, window_end)

    # -- Scénarios -----------------------------------------------------------

    def add_scenario(self, scenario: AnomalyScenario) -> None:
        if scenario.output_id not in self._profiles:
            raise ValueError(f"Sortie inconnue pour le scénario : {scenario.output_id}")
        with self._lock:
            self._scenarios.append(scenario)

    def ground_truth(self) -> list[AnomalyScenario]:
        """
        Vérité terrain complète (§7). À usage exclusif de l'évaluation
        (§33) — ne doit jamais être transmise au détecteur pendant son
        fonctionnement normal.
        """
        with self._lock:
            return list(self._scenarios)

    # -- Génération interne ---------------------------------------------------

    def _generate_range(self, start: datetime, end: datetime) -> list[TrafficSample]:
        if end <= start:
            return []

        n_steps = int((end - start) / self._interval)
        timestamps = [start + i * self._interval for i in range(n_steps)]
        if not timestamps:
            return []

        all_samples: list[TrafficSample] = []
        for profile in self._profiles.values():
            traffic, loss = self._simulate_output(profile, timestamps)
            for ts, tr, lo in zip(timestamps, traffic, loss, strict=True):
                all_samples.append(
                    TrafficSample(
                        timestamp=ts,
                        output_id=profile.output_id,
                        traffic_mbps=float(tr),
                        loss_percent=float(lo),
                    )
                )

        all_samples.sort(key=lambda s: (s.timestamp, s.output_id))
        return all_samples

    def _simulate_output(
        self, profile: OutputProfile, timestamps: list[datetime]
    ) -> tuple[np.ndarray, np.ndarray]:
        hours = np.array([t.hour + t.minute / 60.0 for t in timestamps])
        weekdays = np.array([t.weekday() for t in timestamps])
        is_weekend = weekdays >= 5

        daily_mult = 1.0 + profile.daily_amplitude * np.cos(
            2 * np.pi * (hours - profile.daily_peak_hour) / 24.0
        )
        weekly_mult = np.where(is_weekend, profile.weekend_factor, 1.0)

        traffic_noise = np.exp(
            self._rng.normal(0.0, profile.traffic_noise_std, size=len(timestamps))
        )
        base_traffic = profile.base_level_mbps * daily_mult * weekly_mult * traffic_noise
        base_traffic = np.clip(base_traffic, 0.5, None)

        loss_noise = self._rng.normal(0.0, profile.loss_noise_std, size=len(timestamps))
        base_loss = np.clip(profile.base_loss_percent + loss_noise, 0.0, 100.0)

        traffic = base_traffic.copy()
        loss = base_loss.copy()

        for scenario in self._scenarios:
            if scenario.output_id != profile.output_id:
                continue

            mask = np.array(
                [scenario.start_time <= t < scenario.end_time for t in timestamps]
            )
            if not mask.any():
                continue

            total_duration = (scenario.end_time - scenario.start_time).total_seconds()
            elapsed = np.array(
                [(t - scenario.start_time).total_seconds() / total_duration for t in timestamps]
            )
            traffic_factor, loss_offset = _scenario_effect(scenario, elapsed)

            traffic = np.where(mask, traffic * traffic_factor, traffic)
            loss = np.where(mask, np.clip(loss + loss_offset, 0.0, 100.0), loss)

        traffic = np.clip(traffic, 0.0, None)
        return traffic, loss
