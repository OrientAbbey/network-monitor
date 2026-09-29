"""
Détection d'anomalies (§14-§16, §25).

Normalisation unique et partagée (§16.1) : score = min(1, écart / référence),
jamais un z-score classique — plus robuste aux queues épaisses et
directement interprétable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from network_monitor.app.models import AnomalyResult, ForecastResult, TrafficSample
from network_monitor.analysis.forecasting import compute_residual

# Même quantile que l'intervalle de prévision (§13) : une bande "normale"
# à ~80% de couverture, pas un seuil à 1 dispersion (trop sensible au bruit).
_LOSS_BAND_Z = 1.2816


def normalized_score(deviation: float, reference: float) -> float:
    """Normalisation commune (§16.1) : borne l'écart entre 0 et 1."""
    if reference <= 0:
        return 1.0 if deviation != 0 else 0.0
    return float(min(1.0, abs(deviation) / reference))


@dataclass
class LossBaseline:
    """
    Baseline robuste des pertes pour une sortie (§15) : niveau habituel
    (médiane) et dispersion (MAD), avec un plancher pour éviter qu'une
    sortie très stable ne devienne hypersensible.
    """

    median_loss_percent: float
    dispersion: float  # déjà bornée par loss_std_floor

    @classmethod
    def from_history(cls, history: list[TrafficSample], loss_std_floor: float) -> "LossBaseline":
        if not history:
            raise ValueError("Historique vide : impossible de calibrer la baseline des pertes")
        values = np.array([s.loss_percent for s in history])
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median))) * 1.4826
        return cls(median_loss_percent=median, dispersion=max(mad, loss_std_floor))


def detect_traffic_anomaly(sample: TrafficSample, forecast: ForecastResult) -> tuple[float, float, bool]:
    """
    Détection trafic (§14) : renvoie (score, écart_relatif_%, hors_intervalle).
    Une variation à l'intérieur de l'intervalle prédictif n'est pas une
    anomalie (§13) : le score n'est non nul qu'en dehors de l'intervalle.
    """
    residual = compute_residual(sample, forecast)
    if not residual.out_of_interval:
        return 0.0, residual.relative_residual * 100.0, False

    half_width = residual.interval_width_mbps / 2.0
    if sample.traffic_mbps < forecast.lower_bound_mbps:
        excess = forecast.lower_bound_mbps - sample.traffic_mbps
    else:
        excess = sample.traffic_mbps - forecast.upper_bound_mbps
    score = normalized_score(excess, half_width if half_width > 0 else forecast.predicted_traffic_mbps)
    return score, residual.relative_residual * 100.0, True


def detect_loss_anomaly(sample: TrafficSample, baseline: LossBaseline) -> tuple[float, float]:
    """
    Détection pertes (§15) : renvoie (score, écart en points de %).

    Même logique que le trafic (§16.1) : une déviation à l'intérieur d'une
    bande "normale" (~80% de couverture, cohérent avec la confiance retenue
    pour le forecasting) n'est pas une anomalie. Score nul en dessous de
    cette bande, croissant ensuite avec l'excès — pas un ratio brut, qui
    saturerait à 1.0 dès qu'un simple bruit dépasse 1 dispersion.
    """
    deviation = sample.loss_percent - baseline.median_loss_percent
    normal_band = _LOSS_BAND_Z * baseline.dispersion
    if abs(deviation) <= normal_band:
        return 0.0, deviation
    excess = abs(deviation) - normal_band
    score = normalized_score(excess, normal_band)
    return score, deviation


@dataclass
class PersistenceTracker:
    """
    Suit, par sortie, le nombre de pas consécutifs où le score combiné
    dépasse un seuil (§22), pour alimenter l'hystérésis du moteur
    d'incidents (§21).
    """

    interval_seconds: float
    _consecutive_steps: dict[str, int] = field(default_factory=dict)

    def update(self, output_id: str, joint_score: float, threshold: float) -> float:
        """Met à jour le compteur et renvoie la durée persistante en secondes."""
        if joint_score >= threshold:
            self._consecutive_steps[output_id] = self._consecutive_steps.get(output_id, 0) + 1
        else:
            self._consecutive_steps[output_id] = 0
        return self._consecutive_steps[output_id] * self.interval_seconds


def combine_scores(traffic_score: float, loss_score: float) -> float:
    """
    Score combiné (§16, §25) : le maximum domine, mais la co-occurrence
    renforce la confiance (cas 3 du §25 : trafic↓↓↓ + pertes↑↑↑) sans jamais
    dépasser 1 ni supprimer les scores individuels.
    """
    base = max(traffic_score, loss_score)
    both_significant = min(traffic_score, loss_score) > 0.3
    boost = 0.15 * min(traffic_score, loss_score) if both_significant else 0.0
    return float(min(1.0, base + boost))


def build_anomaly_result(
    sample: TrafficSample,
    forecast: ForecastResult,
    loss_baseline: LossBaseline,
    persistence: PersistenceTracker,
    open_threshold: float,
    data_gap: bool = False,
    traffic_enabled: bool = True,
    loss_enabled: bool = True,
) -> AnomalyResult:
    """
    Assemble un AnomalyResult complet à partir d'une observation validée (§16).
    `traffic_enabled` / `loss_enabled` reflètent `detection.traffic.enabled` et
    `detection.loss.enabled` (§35) : un détecteur désactivé contribue un score nul.
    """
    if traffic_enabled:
        traffic_score, traffic_deviation_pct, out_of_interval = detect_traffic_anomaly(sample, forecast)
    else:
        traffic_score, traffic_deviation_pct, out_of_interval = 0.0, 0.0, False
    if loss_enabled:
        loss_score, loss_deviation_pts = detect_loss_anomaly(sample, loss_baseline)
    else:
        loss_score, loss_deviation_pts = 0.0, 0.0
    joint_score = combine_scores(traffic_score, loss_score)

    duration_seconds = persistence.update(sample.output_id, joint_score, open_threshold)
    max_persistence_seconds = 3600.0  # au-delà d'1h, la persistance est considérée maximale
    persistence_score = float(min(1.0, duration_seconds / max_persistence_seconds))

    return AnomalyResult(
        output_id=sample.output_id,
        timestamp=sample.timestamp,
        traffic_anomaly_score=traffic_score,
        loss_anomaly_score=loss_score,
        persistence_score=persistence_score,
        joint_anomaly_score=joint_score,
        traffic_deviation_percent=traffic_deviation_pct,
        loss_deviation_points=loss_deviation_pts,
        traffic_out_of_interval=out_of_interval,
        data_gap=data_gap,
    )
