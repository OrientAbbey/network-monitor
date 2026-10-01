"""
Features V1 (§9.1) : celles que la détection consomme réellement.

`traffic_vs_expected` et `loss_vs_expected` contextualisent la mesure brute
par rapport à l'attendu ; `rolling_std_1h` mesure la volatilité récente ;
`hour` et `day_of_week` situent l'observation dans le cycle saisonnier.
`recent` contient les mesures *précédentes* de la sortie (la plus ancienne
d'abord), sans la mesure courante.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import numpy as np

from network_monitor.app.models import TrafficSample


def history_steps(interval: timedelta, minutes: int = 60) -> int:
    """Nombre de pas de `interval` couvrant `minutes` (au moins 1)."""
    return max(1, round(minutes * 60 / interval.total_seconds()))


def compute_features(
    sample: TrafficSample,
    expected_traffic_mbps: float,
    expected_loss_percent: float,
    recent: Sequence[TrafficSample],
) -> dict[str, float]:
    window = [s.traffic_mbps for s in recent] + [sample.traffic_mbps]
    ratio = sample.traffic_mbps / expected_traffic_mbps if expected_traffic_mbps > 0 else 1.0
    return {
        "traffic_vs_expected": float(ratio),
        "loss_vs_expected": float(sample.loss_percent - expected_loss_percent),
        "rolling_std_1h": float(np.std(window)) if len(window) > 1 else 0.0,
        "hour": float(sample.timestamp.hour),
        "day_of_week": float(sample.timestamp.weekday()),
    }
