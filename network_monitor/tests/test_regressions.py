"""Régressions des correctifs de la revue : chaque test échouait avant le correctif."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from pydantic import ValidationError

from network_monitor.analysis.forecasting import HoltWintersModel
from network_monitor.app.config import AppConfig, IncidentConfig, _parse_duration
from network_monitor.app.models import IncidentType, RawMeasurement, TrafficSample
from network_monitor.data.source import DataValidator
from network_monitor.incidents.manager import _classify_type

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


def test_holt_winters_prediction_follows_from_time():
    """Avant correctif : predict() renvoyait la même valeur quel que soit from_time."""
    history = [
        TrafficSample(timestamp=T0 + i * STEP, output_id="O", traffic_mbps=1000 + 500 * np.sin(2 * np.pi * i / 288), loss_percent=0.1)
        for i in range(288 * 10)
    ]
    model = HoltWintersModel("O")
    model.fit(history)
    end = history[-1].timestamp
    now = model.predict(end, STEP, STEP)[0].predicted_traffic_mbps
    six_hours_later = model.predict(end + timedelta(hours=6), STEP, STEP)[0].predicted_traffic_mbps
    assert abs(six_hours_later - now) > 200


def test_missing_slots_are_emitted_once():
    """Avant correctif : un trou de 5 pas produisait 1+2+3+4+5 = 15 créneaux."""
    validator = DataValidator(["O"], STEP)
    validator.validate_batch([RawMeasurement(timestamp=T0, output_id="O", traffic_mbps=1, loss_percent=0)], now=T0)
    emitted = sum(len(validator.missing_slots("O", T0 + k * STEP)) for k in range(1, 6))
    assert emitted == 5


@pytest.mark.parametrize("value,expected", [
    ("5m", timedelta(minutes=5)), ("10 minutes", timedelta(minutes=10)),
    ("2days", timedelta(days=2)), ("1.5h", timedelta(hours=1.5)), (90, timedelta(seconds=90)),
])
def test_parse_duration_accepts_common_forms(value, expected):
    assert _parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "10 parsecs"])
def test_parse_duration_rejects_garbage_with_clear_error(value):
    with pytest.raises(ValueError, match="Durée|Unité"):
        _parse_duration(value)


def test_warning_score_must_sit_between_close_and_open():
    with pytest.raises(ValidationError):
        IncidentConfig(open_score=0.7, warning_score=0.9)
    with pytest.raises(ValidationError):
        IncidentConfig(warning_score=0.2, close_score=0.4)


def test_rising_traffic_with_loss_is_congestion_not_a_drop():
    assert _classify_type(0.8, 0.8, +60.0) == IncidentType.CONGESTION
    assert _classify_type(0.8, 0.8, -60.0) == IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS


def test_config_from_yaml_roundtrip(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("incident:\n  open_score: 0.8\n  minimum_duration: 15 minutes\n", encoding="utf-8")
    cfg = AppConfig.from_yaml(path)
    assert cfg.incident.open_score == 0.8
    assert cfg.incident.minimum_duration == timedelta(minutes=15)


def test_evaluation_metrics_on_known_cases():
    from network_monitor.analysis.evaluation import evaluate
    from network_monitor.app.models import Incident, IncidentStatus, Severity
    from network_monitor.data.mock import AnomalyScenario, ScenarioType

    sc = AnomalyScenario(scenario_id="S", output_id="O", start_time=T0, end_time=T0 + timedelta(minutes=40),
                         type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL)

    def inc(id_, out, open_min, close_min):
        return Incident(id=id_, output_id=out, status=IncidentStatus.CLOSED, severity=Severity.CRITICAL,
                        incident_type=IncidentType.TRAFFIC_DROP, score=0.9, opened_at=T0 + timedelta(minutes=open_min),
                        closed_at=T0 + timedelta(minutes=close_min), last_seen_at=T0 + timedelta(minutes=close_min))

    m = evaluate([sc], [inc("a", "O", 10, 50), inc("b", "O", 5000, 5100)], observed_days=1)
    assert (m["detected"], m["false_positives"], m["precision"], m["recall"]) == (1, 1, 0.5, 1.0)
    assert m["mean_detection_delay_min"] == 10.0
