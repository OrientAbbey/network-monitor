"""Tests unitaires de la détection (§14-§16)."""

from datetime import datetime, timezone

from network_monitor.analysis.detection import (
    LossBaseline,
    PersistenceTracker,
    combine_scores,
    detect_loss_anomaly,
    detect_traffic_anomaly,
    normalized_score,
)
from network_monitor.app.models import ForecastModelName, ForecastResult, TrafficSample

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)


def _forecast(predicted=100.0, lower=90.0, upper=110.0):
    return ForecastResult(
        output_id="OUT_001", timestamp=T0, predicted_traffic_mbps=predicted,
        lower_bound_mbps=lower, upper_bound_mbps=upper, confidence=0.8,
        model_name=ForecastModelName.SEASONAL_BASELINE,
    )


def _sample(traffic=100.0, loss=0.1):
    return TrafficSample(timestamp=T0, output_id="OUT_001", traffic_mbps=traffic, loss_percent=loss)


# --- normalized_score ---------------------------------------------------


def test_normalized_score_is_bounded_and_monotonic():
    assert normalized_score(0.0, 10.0) == 0.0
    assert normalized_score(5.0, 10.0) == 0.5
    assert normalized_score(50.0, 10.0) == 1.0  # jamais > 1


def test_normalized_score_handles_zero_reference():
    assert normalized_score(0.0, 0.0) == 0.0
    assert normalized_score(1.0, 0.0) == 1.0


# --- detect_traffic_anomaly ----------------------------------------------


def test_traffic_inside_interval_has_zero_score():
    score, _, out = detect_traffic_anomaly(_sample(traffic=105.0), _forecast())
    assert score == 0.0
    assert out is False


def test_traffic_outside_interval_has_positive_score():
    score, deviation_pct, out = detect_traffic_anomaly(_sample(traffic=40.0), _forecast())
    assert out is True
    assert score > 0.0
    assert deviation_pct < 0  # baisse de trafic


# --- detect_loss_anomaly : non-régression sur la saturation (§15) --------


def test_loss_within_normal_band_has_zero_score():
    """Une dispersion habituelle (bruit gaussien) ne doit pas déclencher de score."""
    baseline = LossBaseline(median_loss_percent=0.15, dispersion=0.06)
    # Une déviation d'exactement 1 dispersion est un bruit tout à fait normal
    # (à peu près 1 écart-type) : elle ne doit PAS saturer le score à 1.0.
    score, deviation = detect_loss_anomaly(_sample(loss=0.15 + 0.06), baseline)
    assert score == 0.0
    assert deviation == 0.06


def test_loss_far_beyond_dispersion_has_high_score():
    baseline = LossBaseline(median_loss_percent=0.15, dispersion=0.06)
    score, _ = detect_loss_anomaly(_sample(loss=9.0), baseline)
    assert score > 0.8


def test_loss_baseline_dispersion_is_floored():
    """Une sortie à variance quasi nulle ne doit pas devenir hypersensible (§15)."""
    history = [_sample(loss=0.05) for _ in range(50)]  # dispersion historique ~0
    baseline = LossBaseline.from_history(history, loss_std_floor=0.05)
    assert baseline.dispersion >= 0.05
    # Une perte légèrement différente (0.1pt) reste dans la bande normale grâce au plancher.
    score, _ = detect_loss_anomaly(_sample(loss=0.1), baseline)
    assert score == 0.0


# --- combine_scores --------------------------------------------------------


def test_combine_scores_takes_the_max_when_only_one_is_significant():
    assert combine_scores(0.9, 0.1) == 0.9


def test_combine_scores_boosts_when_both_are_significant():
    boosted = combine_scores(0.6, 0.5)
    assert boosted > max(0.6, 0.5)
    assert boosted <= 1.0


# --- PersistenceTracker ------------------------------------------------------


def test_persistence_tracker_accumulates_then_resets():
    tracker = PersistenceTracker(interval_seconds=300.0)  # 5 minutes
    assert tracker.update("OUT_001", 0.8, threshold=0.75) == 300.0
    assert tracker.update("OUT_001", 0.8, threshold=0.75) == 600.0
    assert tracker.update("OUT_001", 0.2, threshold=0.75) == 0.0  # repasse sous le seuil


def test_persistence_tracker_is_independent_per_output():
    tracker = PersistenceTracker(interval_seconds=300.0)
    tracker.update("OUT_001", 0.9, threshold=0.75)
    assert tracker.update("OUT_002", 0.9, threshold=0.75) == 300.0
