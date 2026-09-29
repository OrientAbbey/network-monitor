"""Tests de la configuration (§35) : chaque option est soit effective, soit explicitement refusée."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from network_monitor.analysis.detection import LossBaseline, PersistenceTracker, build_anomaly_result
from network_monitor.analysis.forecasting import HoltWintersModel, SeasonalBaselineModel, select_model
from network_monitor.app.config import AppConfig
from network_monitor.app.models import AnomalyResult, ForecastModelName, ForecastResult, TrafficSample
from network_monitor.data.mock import MockDataSource
from network_monitor.incidents.manager import IncidentManager

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


def test_disabling_traffic_detection_zeroes_traffic_score():
    tracker = PersistenceTracker(300.0)
    r = build_anomaly_result(_bad_sample(), _forecast(), _baseline(), tracker, 0.75, traffic_enabled=False)
    assert r.traffic_anomaly_score == 0.0 and r.loss_anomaly_score > 0.0


def test_disabling_loss_detection_zeroes_loss_score():
    tracker = PersistenceTracker(300.0)
    r = build_anomaly_result(_bad_sample(), _forecast(), _baseline(), tracker, 0.75, loss_enabled=False)
    assert r.loss_anomaly_score == 0.0 and r.traffic_anomaly_score > 0.0


def test_both_detectors_enabled_by_default():
    tracker = PersistenceTracker(300.0)
    r = build_anomaly_result(_bad_sample(), _forecast(), _baseline(), tracker, 0.75)
    assert r.traffic_anomaly_score > 0.0 and r.loss_anomaly_score > 0.0


@pytest.mark.parametrize("section", ["isolation_forest", "extended_features"])
def test_unimplemented_detection_options_are_refused_when_enabled(section):
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"detection": {section: {"enabled": True}}})
    AppConfig.model_validate({"detection": {section: {"enabled": False}}})  # désactivé : accepté


def test_unknown_forecast_model_is_refused():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"forecast": {"models": ["prophet"]}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"forecast": {"models": []}})


def test_select_model_honours_allowed_models():
    src = MockDataSource(seed=1)
    now = datetime(2026, 9, 1, tzinfo=UTC)
    hist = [s for s in src.load_history(now - timedelta(days=50), now) if s.output_id == "OUT_001"]
    only_baseline = select_model("OUT_001", hist, timedelta(days=45), timedelta(days=7),
                                 allowed_models=["seasonal_baseline"])
    assert isinstance(only_baseline, SeasonalBaselineModel)
    only_hw = select_model("OUT_001", hist, timedelta(days=45), timedelta(days=7),
                           allowed_models=["holt_winters"])
    assert isinstance(only_hw, (HoltWintersModel, SeasonalBaselineModel))  # repli baseline si HW échoue au backtest


def test_alerts_disabled_produces_no_alerts_but_incidents_still_evolve():
    cfg = AppConfig()
    mgr_on = IncidentManager(cfg.incident, alerts_enabled=True)
    mgr_off = IncidentManager(cfg.incident, alerts_enabled=False)
    events_on, events_off = 0, 0
    for i in range(6):
        res = AnomalyResult(output_id="O", timestamp=T0 + i * timedelta(minutes=5),
                            traffic_anomaly_score=0.9, loss_anomaly_score=0.9, joint_anomaly_score=0.9)
        a_on, inc_on = mgr_on.process(res)
        a_off, inc_off = mgr_off.process(res)
        events_on += len(a_on)
        events_off += len(a_off)
    assert events_on > 0 and events_off == 0
    assert inc_on is not None and inc_off is not None
    assert inc_on.status == inc_off.status  # le cycle de vie n'est pas affecté


def test_alerts_warning_score_key_in_legacy_yaml_is_ignored():
    cfg = AppConfig.model_validate({"alerts": {"enabled": True, "warning_score": 0.5, "critical_score": 0.9}})
    assert cfg.alerts.critical_score == 0.9
