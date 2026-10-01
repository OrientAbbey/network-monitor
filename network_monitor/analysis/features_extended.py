"""
Features étendues (§9.2), isolées du pipeline V1 : calculées seulement si
`detection.extended_features.enabled` est activé, ou par l'Isolation Forest
(§17), qui est leur seul consommateur.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import numpy as np

from network_monitor.analysis.features import history_steps
from network_monitor.app.models import TrafficSample

_TRAFFIC_CHANGE_MINUTES = {"5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}
_LOSS_CHANGE_MINUTES = {"5m": 5, "15m": 15, "30m": 30}
_ROLLING_MEAN_MINUTES = {"15m": 15, "1h": 60}

EXTENDED_KEYS = (
    *(f"traffic_change_{k}" for k in _TRAFFIC_CHANGE_MINUTES),
    *(f"loss_change_{k}" for k in _LOSS_CHANGE_MINUTES),
    *(f"rolling_mean_{k}" for k in _ROLLING_MEAN_MINUTES),
    "rolling_median_1h",
)


def compute_extended_features(
    sample: TrafficSample, recent: Sequence[TrafficSample], interval: timedelta
) -> dict[str, float] | None:
    """Retourne None tant que `recent` ne couvre pas une heure complète."""
    need = history_steps(interval)
    if len(recent) < need:
        return None
    recent = list(recent)  # un deque n'accepte pas le découpage
    feats: dict[str, float] = {"minute": float(sample.timestamp.minute)}
    for key, minutes in _TRAFFIC_CHANGE_MINUTES.items():
        feats[f"traffic_change_{key}"] = sample.traffic_mbps - recent[-history_steps(interval, minutes)].traffic_mbps
    for key, minutes in _LOSS_CHANGE_MINUTES.items():
        feats[f"loss_change_{key}"] = sample.loss_percent - recent[-history_steps(interval, minutes)].loss_percent
    for key, minutes in _ROLLING_MEAN_MINUTES.items():
        window = [s.traffic_mbps for s in recent[-history_steps(interval, minutes):]] + [sample.traffic_mbps]
        feats[f"rolling_mean_{key}"] = float(np.mean(window))
    hour_window = [s.traffic_mbps for s in recent[-need:]] + [sample.traffic_mbps]
    feats["rolling_median_1h"] = float(np.median(hour_window))
    return feats
