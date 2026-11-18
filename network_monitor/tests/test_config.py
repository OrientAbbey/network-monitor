"""Tests de la configuration : chaque option est soit effective, soit explicitement refusée."""

from datetime import UTC, datetime, timedelta

from network_monitor.analysis.detection import LossBaseline, PersistenceTracker, build_anomaly_result
from network_monitor.analysis.forecasting import SeasonalBaselineModel, select_model
from network_monitor.app.config import AppConfig
from network_monitor.app.models import ForecastModelName, ForecastResult, TrafficSample
from network_monitor.data.mock import MockDataSource

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _forecast(pred=100.0):
    return ForecastResult(
        output_id="O", timestamp=T0, predicted_traffic_mbps=pred, lower_bound_mbps=pred * 0.9,
        upper_bound_mbps=pred * 1.1, confidence=0.8, model_name=ForecastModelName.SEASONAL_BASELINE,
    )


def _baseline():
    hist = [TrafficSample(timestamp=T0 + i * timedelta(minutes=5), output_id="O", traffic_mbps=100.0,
                          loss_percent=0.1) for i in range(500)]
    return LossBaseline.from_history(hist, 0.05)


def _bad_sample():
    return TrafficSample(timestamp=T0, output_id="O", traffic_mbps=5.0, loss_percent=20.0)


def test_disabling_loss_detection_zeroes_loss_score():
    tracker = PersistenceTracker(300.0)
    r = build_anomaly_result(_bad_sample(), _forecast(), _baseline(), tracker, 0.75, loss_enabled=False)
    assert r.loss_anomaly_score == 0.0 and r.traffic_anomaly_score > 0.0


def test_both_detectors_enabled_by_default():
    tracker = PersistenceTracker(300.0)
    r = build_anomaly_result(_bad_sample(), _forecast(), _baseline(), tracker, 0.75)
    assert r.traffic_anomaly_score > 0.0 and r.loss_anomaly_score > 0.0


def test_forecast_models_option_restricts_the_candidates():
    src = MockDataSource(seed=1)
    hist = [s for s in src.load_history(T0 - timedelta(days=50), T0) if s.output_id == "OUT_001"]

    only_baseline = select_model(
        "OUT_001", hist, timedelta(days=45), timedelta(days=7), allowed_models=["seasonal_baseline"]
    )
    assert isinstance(only_baseline, SeasonalBaselineModel)

    both = select_model("OUT_001", hist, timedelta(days=45), timedelta(days=7))
    assert both is not None

    cfg = AppConfig.model_validate({"forecast": {"models": ["seasonal_baseline"]}})
    assert cfg.forecast.models == ["seasonal_baseline"]


def test_alerts_warning_score_key_in_legacy_yaml_is_ignored():
    cfg = AppConfig.model_validate({"alerts": {"enabled": True, "warning_score": 0.5, "critical_score": 0.9}})
    assert cfg.alerts.critical_score == 0.9


def test_recalibration_and_retention_defaults():
    cfg = AppConfig()
    assert cfg.recalibration.enabled is True
    assert cfg.recalibration.interval == timedelta(hours=24)
    assert cfg.history.preferred == timedelta(days=90)
