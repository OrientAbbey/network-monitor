"""Tests de non-régression : chacun échoue sans le correctif correspondant."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from network_monitor.analysis.forecasting import HoltWintersModel
from network_monitor.app.config import AppConfig, IncidentConfig, _parse_duration
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import (
    AnomalyResult,
    DataQualityFlag,
    ForecastModelName,
    ForecastResult,
    Incident,
    IncidentStatus,
    IncidentType,
    RawMeasurement,
    Severity,
    TrafficSample,
)
from network_monitor.data.mock import MockDataSource
from network_monitor.data.source import DataValidator
from network_monitor.storage.repository import SqlAlchemyRepository

NOW = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
IV = timedelta(minutes=5)


@pytest.fixture
def loop(tmp_path):
    storage = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'reg.db'}")
    src = MockDataSource(seed=3)
    lp = MonitoringLoop(src, storage, AppConfig(history={"minimum": "30d", "preferred": "40d"}))
    lp.calibrate(NOW)
    src.start_realtime(NOW)
    yield lp, src
    storage.dispose()


def _record_flags(lp: MonitoringLoop, monkeypatch) -> list[list[DataQualityFlag]]:
    seen: list[list[DataQualityFlag]] = []
    original = lp._process_validated_sample

    def spy(validated):
        seen.append(list(validated.quality_flags))
        return original(validated)

    monkeypatch.setattr(lp, "_process_validated_sample", spy)
    return seen


# --- trous de données : aucun faux positif, les vrais trous détectés une seule fois ---


def test_no_phantom_missing_slot_in_normal_operation(loop, monkeypatch):
    lp, _ = loop
    seen = _record_flags(lp, monkeypatch)
    for k in range(1, 9):
        lp.run_tick(NOW + k * IV)
    assert seen, "des mesures auraient dû être traitées"
    assert all(DataQualityFlag.MISSING not in flags for flags in seen)


def test_real_gap_is_detected_exactly_once(loop, monkeypatch):
    lp, src = loop
    seen = _record_flags(lp, monkeypatch)
    for k in range(1, 4):
        lp.run_tick(NOW + k * IV)
    assert all(DataQualityFlag.MISSING not in f for f in seen)

    # Panne de collecte de 2 pas : la source avance mais rien n'est transmis.
    src.get_latest()
    src.get_latest()
    seen.clear()
    lp.run_tick(NOW + 6 * IV)  # reprise
    missing = [f for f in seen if DataQualityFlag.MISSING in f]
    assert len(missing) == 2 * len(src.list_outputs())

    seen.clear()
    lp.run_tick(NOW + 7 * IV)  # tick normal suivant : le trou ne doit pas être re-signalé
    assert all(DataQualityFlag.MISSING not in f for f in seen)


# --- un doublon n'est pas un trou de données ---


def test_duplicate_measurement_is_not_treated_as_data_gap(loop):
    lp, _ = loop
    validator = DataValidator(["OUT_001"], IV)
    m = RawMeasurement(timestamp=NOW, output_id="OUT_001", traffic_mbps=100.0, loss_percent=0.1)
    validator.validate_batch([m], now=NOW + IV)
    duplicate = validator.validate_batch([m], now=NOW + IV)[0]
    assert DataQualityFlag.DUPLICATE in duplicate.quality_flags

    assert lp._process_validated_sample(duplicate) == (None, None, None, [])
    # aucun incident (ni preuve `data_gap`) n'a pu être créé par ce doublon
    assert lp.incident_manager.active_incidents() == []


# --- validation de la configuration ---


@pytest.mark.parametrize(
    "kwargs",
    [
        {"open_score": 0.75, "close_score": 0.40, "warning_score": 0.90},  # warning > open
        {"open_score": 0.75, "close_score": 0.60, "warning_score": 0.50},  # close > warning
        {"open_score": 0.75, "close_score": 0.40, "warning_score": 0.75},  # warning == open
        {"open_score": 0.75, "close_score": 0.50, "warning_score": 0.50},  # warning == close
    ],
)
def test_incident_config_rejects_unordered_thresholds(kwargs):
    with pytest.raises(ValidationError):
        IncidentConfig(**kwargs)


def test_incident_config_accepts_ordered_thresholds():
    cfg = IncidentConfig(open_score=0.8, close_score=0.3, warning_score=0.5)
    assert cfg.close_score < cfg.warning_score < cfg.open_score


@pytest.mark.parametrize("bad", ["-5m", -60, "abc", "5x", True])
def test_parse_duration_rejects_invalid_values(bad):
    with pytest.raises(ValueError):
        _parse_duration(bad)


@pytest.mark.parametrize(
    "value,expected",
    [("5m", timedelta(minutes=5)), ("10min", timedelta(minutes=10)), ("2h", timedelta(hours=2)),
     ("30d", timedelta(days=30)), ("0", timedelta(0)), (90, timedelta(seconds=90))],
)
def test_parse_duration_accepts_valid_values(value, expected):
    assert _parse_duration(value) == expected


def test_incident_config_rejects_negative_timedelta_and_string():
    with pytest.raises(ValidationError):
        IncidentConfig(minimum_duration=timedelta(minutes=-5))
    with pytest.raises(ValidationError):
        IncidentConfig(minimum_duration="-5m")


# --- Holt-Winters doit respecter `from_time` ---


def _seasonal_history(days: int = 4) -> list[TrafficSample]:
    n = int(timedelta(days=days) / IV)
    return [
        TrafficSample(
            timestamp=NOW - (n - i) * IV,
            output_id="OUT_001",
            traffic_mbps=200.0 + 100.0 * math.sin(2 * math.pi * i / 288),
            loss_percent=0.1,
        )
        for i in range(n)
    ]


def test_holt_winters_prediction_follows_from_time():
    history = _seasonal_history()
    model = HoltWintersModel("OUT_001")
    model.fit(history)
    end = history[-1].timestamp

    # Même prévision « 1 pas » demandée à des heures très différentes de la journée.
    values = [
        model.predict(end + timedelta(hours=h) - IV + IV, IV, IV)[0].predicted_traffic_mbps
        for h in (0, 6, 12, 18)
    ]
    assert max(values) - min(values) > 50.0, f"prévision plate malgré la saisonnalité : {values}"


def test_holt_winters_single_step_matches_multi_step_slice():
    history = _seasonal_history()
    model = HoltWintersModel("OUT_001")
    model.fit(history)
    end = history[-1].timestamp
    long = model.predict(end, timedelta(hours=3), IV)
    later = model.predict(end + timedelta(hours=2), IV, IV)[0]
    assert later.timestamp == long[24].timestamp
    assert later.predicted_traffic_mbps == pytest.approx(long[24].predicted_traffic_mbps)
    assert later.model_name == ForecastModelName.HOLT_WINTERS


def test_holt_winters_rejects_from_time_inside_training_range():
    history = _seasonal_history()
    model = HoltWintersModel("OUT_001")
    model.fit(history)
    with pytest.raises(ValueError):
        model.predict(history[-1].timestamp - timedelta(hours=1), IV, IV)


# --- Stockage : transaction unique, WAL, dispose ------------------------------


def test_sqlite_uses_wal_journal(tmp_path):
    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'wal.db'}")
    try:
        with repo._engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    finally:
        repo.dispose()


def test_save_tick_persists_everything_in_one_call(tmp_path):
    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'tick.db'}")
    try:
        sample = TrafficSample(timestamp=NOW, output_id="OUT_001", traffic_mbps=120.0, loss_percent=0.2)
        forecast = ForecastResult(
            output_id="OUT_001", timestamp=NOW, predicted_traffic_mbps=110.0, lower_bound_mbps=90.0,
            upper_bound_mbps=130.0, confidence=0.8, model_name=ForecastModelName.SEASONAL_BASELINE,
        )
        anomaly = AnomalyResult(
            output_id="OUT_001", timestamp=NOW, traffic_anomaly_score=0.1, loss_anomaly_score=0.0,
            joint_anomaly_score=0.1,
        )
        incident = Incident(
            id="inc-1", output_id="OUT_001", status=IncidentStatus.OPEN, severity=Severity.WARNING,
            incident_type=IncidentType.TRAFFIC_DROP, suspected_at=NOW, opened_at=NOW, last_seen_at=NOW,
            score=0.8, description="test",
        )
        repo.save_tick([sample], [forecast], [anomaly], [incident])

        window = (NOW - IV, NOW + IV)
        assert len(repo.get_history("OUT_001", *window)) == 1
        assert len(repo.get_forecasts("OUT_001", *window)) == 1
        assert len(repo.get_anomalies("OUT_001", *window)) == 1
        assert repo.get_incident("inc-1") is not None

        repo.save_tick([sample], [forecast], [anomaly], [incident])  # idempotent
        assert len(repo.get_history("OUT_001", *window)) == 1
    finally:
        repo.dispose()
