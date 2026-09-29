"""Tests unitaires du forecasting (§10-§13)."""

from datetime import UTC, datetime, timedelta

import pytest

from network_monitor.analysis.forecasting import (
    HoltWintersModel,
    SeasonalBaselineModel,
    compute_residual,
    select_model,
)
from network_monitor.app.models import ForecastModelName, ForecastResult, TrafficSample

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _flat_history(days: int, level: float = 100.0, interval=timedelta(minutes=5)) -> list[TrafficSample]:
    """Historique constant (aucune saisonnalité) : suffisant pour tester la mécanique,
    pas le réalisme du signal (couvert par les tests de scénarios)."""
    n = int(timedelta(days=days) / interval)
    return [
        TrafficSample(timestamp=T0 + i * interval, output_id="OUT_001", traffic_mbps=level, loss_percent=0.1)
        for i in range(n)
    ]


def test_seasonal_baseline_predicts_flat_history():
    model = SeasonalBaselineModel("OUT_001")
    model.fit(_flat_history(days=10))
    forecasts = model.predict(T0 + timedelta(days=9), timedelta(minutes=15), timedelta(minutes=5))
    assert len(forecasts) == 3
    assert all(f.predicted_traffic_mbps == pytest.approx(100.0, abs=1.0) for f in forecasts)


def test_seasonal_baseline_rejects_empty_history():
    with pytest.raises(ValueError):
        SeasonalBaselineModel("OUT_001").fit([])


def test_holt_winters_rejects_insufficient_history():
    """Moins de 2 cycles quotidiens ne doit pas produire un modèle silencieusement faux (§10.B)."""
    with pytest.raises(ValueError):
        HoltWintersModel("OUT_001").fit(_flat_history(days=1))


def test_select_model_falls_back_to_baseline_below_minimum_history():
    """En dessous du seuil configuré, pas de backtesting : repli automatique (§11)."""
    history = _flat_history(days=20)
    model = select_model(
        "OUT_001", history,
        minimum_history_for_selection=timedelta(days=45),
        validation_window=timedelta(days=7),
    )
    assert model.name == ForecastModelName.SEASONAL_BASELINE


def test_select_model_handles_empty_history():
    with pytest.raises(ValueError):
        select_model("OUT_001", [], timedelta(days=45), timedelta(days=7))


# --- compute_residual --------------------------------------------------


def test_compute_residual_flags_out_of_interval():
    forecast = ForecastResult(
        output_id="OUT_001", timestamp=T0, predicted_traffic_mbps=100.0,
        lower_bound_mbps=90.0, upper_bound_mbps=110.0, confidence=0.8,
        model_name=ForecastModelName.SEASONAL_BASELINE,
    )
    inside = TrafficSample(timestamp=T0, output_id="OUT_001", traffic_mbps=95.0, loss_percent=0.1)
    outside = TrafficSample(timestamp=T0, output_id="OUT_001", traffic_mbps=50.0, loss_percent=0.1)

    assert compute_residual(inside, forecast).out_of_interval is False
    report = compute_residual(outside, forecast)
    assert report.out_of_interval is True
    assert report.relative_residual == pytest.approx(-0.5, rel=0.01)
