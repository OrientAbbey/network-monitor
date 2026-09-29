"""Recalibration périodique (§32), rétention, alertes persistées, incident.current_score."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from network_monitor.app.config import AppConfig, MonitoringConfig, StorageConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import (
    AlertEvent,
    AnomalyResult,
    Severity,
    TrafficSample,
)
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.storage.repository import SqlAlchemyRepository

NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
IV = timedelta(minutes=5)


def _config(**overrides) -> AppConfig:
    base = {"history": {"minimum": "30d", "preferred": "40d"}}
    base.update(overrides)
    return AppConfig.model_validate(base)


def _loop(tmp_path, config, scenarios=()):
    storage = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'm.db'}")
    src = MockDataSource(seed=3)
    for sc in scenarios:
        src.add_scenario(sc)
    loop = MonitoringLoop(src, storage, config)
    loop.calibrate(NOW)
    src.start_realtime(NOW)
    return loop, src, storage


def _drop(start_min=5, minutes=60):
    return AnomalyScenario(
        scenario_id="S", output_id="OUT_001", start_time=NOW + timedelta(minutes=start_min),
        end_time=NOW + timedelta(minutes=start_min + minutes), type=ScenarioType.TRAFFIC_DROP_HIGH_LOSS,
        severity=Severity.CRITICAL, traffic_drop_fraction=0.7, loss_offset_points=10.0,
    )


# --- configuration -----------------------------------------------------------


def test_new_config_defaults_and_validation():
    cfg = AppConfig()
    assert cfg.monitoring.recalibration_interval == timedelta(hours=24)
    assert cfg.storage.retention == timedelta(days=30)
    assert MonitoringConfig(recalibration_interval=None).recalibration_interval is None
    assert StorageConfig(retention=None).retention is None
    for bad in ("0", "-1h"):
        with pytest.raises(ValidationError):
            MonitoringConfig(recalibration_interval=bad)
        with pytest.raises(ValidationError):
            StorageConfig(retention=bad)
    with pytest.raises(ValidationError):
        StorageConfig(purge_interval="0")


# --- recalibration (§32) -----------------------------------------------------


def test_recalibration_triggers_on_schedule_and_is_configurable(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(monitoring={"recalibration_interval": "30m"}))
    try:
        assert loop._last_calibrated_at == NOW
        calls = []
        original = loop.calibrate
        loop.calibrate = lambda now, exclude_windows=None: (calls.append(now), original(now, exclude_windows))
        for k in range(1, 16):
            loop.run_tick(NOW + k * IV)
        assert calls == [NOW + 6 * IV, NOW + 12 * IV]  # toutes les 30 min = 6 pas
    finally:
        storage.dispose()


def test_recalibration_can_be_disabled(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(monitoring={"recalibration_interval": None}))
    try:
        for k in range(1, 20):
            loop.run_tick(NOW + k * IV)
        assert loop._last_calibrated_at == NOW
    finally:
        storage.dispose()


def test_recalibration_excludes_confirmed_incident_windows(tmp_path):
    loop, _, storage = _loop(
        tmp_path, _config(monitoring={"recalibration_interval": "2h"}), scenarios=[_drop()]
    )
    try:
        captured = {}
        original = loop.calibrate

        def spy(now, exclude_windows=None):
            captured["windows"] = exclude_windows
            return original(now, exclude_windows)

        loop.calibrate = spy
        for k in range(1, 26):  # > 2 h
            loop.run_tick(NOW + k * IV)
        windows = captured["windows"]
        assert windows, "l'incident confirmé aurait dû produire une fenêtre d'exclusion"
        # la fenêtre exclue recouvre la période de l'anomalie injectée
        assert any(w0 <= NOW + timedelta(minutes=30) <= w1 for w0, w1 in windows)
    finally:
        storage.dispose()


def test_calibrate_excluding_windows_removes_anomalous_samples_from_the_model(tmp_path):
    """Sans exclusion, une longue anomalie apprise comme « normale » abaisse la prévision ; avec, non."""
    week = timedelta(days=7)
    scenario = AnomalyScenario(
        scenario_id="OLD", output_id="OUT_001", start_time=NOW - week - timedelta(hours=1),
        end_time=NOW - week + timedelta(hours=5), type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL,
        traffic_drop_fraction=0.9,
    )
    loop, src, storage = _loop(tmp_path, _config(), scenarios=[scenario])
    try:
        target = NOW - week  # même jour de semaine/créneau que l'anomalie apprise
        polluted = loop._forecast_models["OUT_001"].predict(target - IV, IV, IV)[0].predicted_traffic_mbps
        loop.calibrate(NOW, exclude_windows=[(scenario.start_time, scenario.end_time)])
        cleaned = loop._forecast_models["OUT_001"].predict(target - IV, IV, IV)[0].predicted_traffic_mbps
        assert cleaned >= polluted  # l'exclusion ne peut pas dégrader la référence normale
    finally:
        storage.dispose()


def test_calibrate_keeps_previous_model_when_too_much_history_is_excluded(tmp_path):
    loop, _, storage = _loop(tmp_path, _config())
    try:
        before = loop._forecast_models["OUT_001"]
        everything = [(NOW - timedelta(days=60), NOW + timedelta(days=1))]
        loop.calibrate(NOW, exclude_windows=everything)
        assert loop._forecast_models["OUT_001"] is before
    finally:
        storage.dispose()


def test_recalibration_failure_keeps_running_with_previous_models(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(monitoring={"recalibration_interval": "15m"}))
    try:
        before = dict(loop._forecast_models)

        def boom(now, exclude_windows=None):
            raise RuntimeError("source indisponible")

        loop.calibrate = boom
        for k in range(1, 9):
            loop.run_tick(NOW + k * IV)  # ne doit pas lever
        assert loop._forecast_models == before
    finally:
        storage.dispose()


# --- alertes persistées (§27) et current_score (§3.6) -----------------------


def test_alerts_are_persisted_with_the_incident_lifecycle(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(), scenarios=[_drop(minutes=40)])
    try:
        for k in range(1, 25):
            loop.run_tick(NOW + k * IV)
        incident = next(i for i in storage.list_incidents(limit=50) if i.output_id == "OUT_001" and i.opened_at)
        alerts = storage.get_alerts(incident_id=incident.id, limit=500)
        events = [a.event for a in reversed(alerts)]
        assert events[0] == AlertEvent.SUSPECT
        assert AlertEvent.OPENED in events and events[-1] == AlertEvent.CLOSED
        assert events.index(AlertEvent.OPENED) < events.index(AlertEvent.CLOSED)
        assert all(a.output_id == "OUT_001" for a in alerts)
        # Les autres sorties peuvent avoir des signaux faibles (SUSPECT puis résorbé),
        # mais aucun incident confirmé : pas d'alerte OPENED.
        other = storage.get_alerts(output_id="OUT_002", limit=200)
        assert all(a.event != AlertEvent.OPENED for a in other)
    finally:
        storage.dispose()


def test_alerts_disabled_persists_nothing_but_incidents_still_recorded(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(alerts={"enabled": False}), scenarios=[_drop(minutes=40)])
    try:
        for k in range(1, 25):
            loop.run_tick(NOW + k * IV)
        assert storage.get_alerts(limit=100) == []
        assert any(i.opened_at for i in storage.list_incidents(limit=50))
    finally:
        storage.dispose()


def test_incident_exposes_peak_score_and_current_score(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(), scenarios=[_drop(minutes=40)])
    try:
        for k in range(1, 25):
            loop.run_tick(NOW + k * IV)
        incident = next(i for i in storage.list_incidents(limit=50) if i.output_id == "OUT_001" and i.opened_at)
        assert incident.current_score is not None
        assert incident.score >= incident.current_score  # pic >= dernier score
    finally:
        storage.dispose()


def test_anomaly_deviation_fields_are_persisted(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(), scenarios=[_drop(minutes=40)])
    try:
        for k in range(1, 15):
            loop.run_tick(NOW + k * IV)
        anomalies = storage.get_anomalies("OUT_001", NOW, NOW + timedelta(hours=2))
        assert any(a.traffic_deviation_percent is not None and a.traffic_deviation_percent < -30 for a in anomalies)
        assert any(a.traffic_out_of_interval for a in anomalies)
        assert any(a.loss_deviation_points is not None and a.loss_deviation_points > 5 for a in anomalies)
    finally:
        storage.dispose()


# --- rétention (item 16) ------------------------------------------------------


def _fill(storage, days):
    samples = [
        TrafficSample(timestamp=NOW - timedelta(days=days) + i * timedelta(hours=1), output_id="OUT_001",
                      traffic_mbps=100.0, loss_percent=0.1)
        for i in range(days * 24)
    ]
    anomalies = [
        AnomalyResult(output_id="OUT_001", timestamp=s.timestamp, traffic_anomaly_score=0.0,
                      loss_anomaly_score=0.0, joint_anomaly_score=0.0)
        for s in samples
    ]
    storage.save_tick(samples, [], anomalies, [])


def test_purge_before_removes_old_rows_only(tmp_path):
    storage = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'p.db'}")
    try:
        _fill(storage, days=10)
        deleted = storage.purge_before(NOW - timedelta(days=4))
        assert deleted["measurements"] == 6 * 24 and deleted["anomalies"] == 6 * 24
        remaining = storage.get_history("OUT_001", NOW - timedelta(days=11), NOW)
        assert len(remaining) == 4 * 24
        assert min(s.timestamp for s in remaining) >= NOW - timedelta(days=4)
    finally:
        storage.dispose()


def test_loop_purges_on_schedule(tmp_path):
    cfg = _config(storage={"retention": "30m", "purge_interval": "10m"}, monitoring={"recalibration_interval": None})
    loop, _, storage = _loop(tmp_path, cfg)
    try:
        for k in range(1, 31):  # 2 h 30
            loop.run_tick(NOW + k * IV)
        oldest = min(s.timestamp for s in storage.get_history("OUT_001", NOW - timedelta(days=1), NOW + timedelta(days=1)))
        assert oldest >= NOW + 30 * IV - timedelta(minutes=30) - timedelta(minutes=10) - IV
    finally:
        storage.dispose()


def test_retention_none_keeps_everything(tmp_path):
    cfg = _config(storage={"retention": None}, monitoring={"recalibration_interval": None})
    loop, _, storage = _loop(tmp_path, cfg)
    try:
        for k in range(1, 20):
            loop.run_tick(NOW + k * IV)
        assert len(storage.get_history("OUT_001", NOW, NOW + timedelta(days=1))) == 19
    finally:
        storage.dispose()


def test_old_schema_database_is_migrated_without_data_loss(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE anomalies (output_id VARCHAR NOT NULL, timestamp DATETIME NOT NULL,
            traffic_anomaly_score FLOAT NOT NULL, loss_anomaly_score FLOAT NOT NULL,
            persistence_score FLOAT NOT NULL, joint_anomaly_score FLOAT NOT NULL,
            data_gap VARCHAR NOT NULL, PRIMARY KEY (output_id, timestamp));
        CREATE TABLE incidents (id VARCHAR NOT NULL, output_id VARCHAR NOT NULL, status VARCHAR NOT NULL,
            severity VARCHAR NOT NULL, incident_type VARCHAR NOT NULL, suspected_at DATETIME, opened_at DATETIME,
            last_seen_at DATETIME NOT NULL, closed_at DATETIME, score FLOAT NOT NULL,
            peak_traffic_deviation_percent FLOAT NOT NULL, peak_loss_percent FLOAT NOT NULL,
            description VARCHAR NOT NULL, evidence JSON NOT NULL, PRIMARY KEY (id));
        INSERT INTO anomalies VALUES ('OUT_001','2026-09-01 00:00:00',0.1,0.0,0.0,0.1,'0');
        INSERT INTO incidents VALUES ('i1','OUT_001','closed','warning','traffic_drop','2026-09-01 00:00:00',
            '2026-09-01 00:10:00','2026-09-01 01:00:00','2026-09-01 01:00:00',0.8,-50,0,'ancien','[]');
        """
    )
    con.commit()
    con.close()
    storage = SqlAlchemyRepository(f"sqlite:///{db}")
    try:
        old = storage.get_incident("i1")
        assert old is not None and old.current_score is None and old.score == 0.8
        (a,) = storage.get_anomalies("OUT_001", NOW - timedelta(days=1), NOW + timedelta(days=1))
        assert a.traffic_deviation_percent is None and a.traffic_out_of_interval is False
        storage.save_alerts([])  # la table alerts a bien été créée
        assert storage.get_alerts() == []
    finally:
        storage.dispose()
