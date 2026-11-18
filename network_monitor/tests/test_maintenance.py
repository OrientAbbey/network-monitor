"""Recalibration périodique, rétention et alertes persistées."""

from datetime import UTC, datetime, timedelta

from network_monitor.app.config import AppConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import Severity, TrafficSample
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.storage.repository import SqlAlchemyRepository

NOW = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
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


def _drop(start_min=5, minutes=40):
    return AnomalyScenario(
        scenario_id="S", output_id="OUT_001", start_time=NOW + timedelta(minutes=start_min),
        end_time=NOW + timedelta(minutes=start_min + minutes), type=ScenarioType.TRAFFIC_DROP_HIGH_LOSS,
        severity=Severity.CRITICAL, traffic_drop_fraction=0.7, loss_offset_points=10.0,
    )


def _old_sample(days=41):
    return TrafficSample(
        timestamp=NOW - timedelta(days=days), output_id="OUT_001", traffic_mbps=1.0, loss_percent=0.0,
    )


# --- configuration -----------------------------------------------------------


def test_recalibration_and_retention_defaults():
    cfg = AppConfig()
    assert cfg.recalibration.enabled is True
    assert cfg.recalibration.interval == timedelta(hours=24)
    assert cfg.history.preferred == timedelta(days=90)


# --- rétention : la purge suit le rythme de la recalibration ------------------


def test_recalibration_purges_series_older_than_the_preferred_history(tmp_path):
    loop, _, storage = _loop(tmp_path, _config())
    try:
        old = _old_sample()
        storage.save_samples([old])
        window = (old.timestamp - timedelta(minutes=5), old.timestamp + timedelta(minutes=5))
        assert len(storage.get_history("OUT_001", *window)) == 1

        loop.maybe_recalibrate(NOW + timedelta(hours=25))

        assert storage.get_history("OUT_001", *window) == []  # 41 j > history.preferred (40 j)
    finally:
        storage.dispose()


def test_disabled_recalibration_does_not_purge(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(recalibration={"enabled": False}))
    try:
        old = _old_sample()
        storage.save_samples([old])
        window = (old.timestamp - timedelta(minutes=5), old.timestamp + timedelta(minutes=5))

        loop.maybe_recalibrate(NOW + timedelta(days=5))

        assert len(storage.get_history("OUT_001", *window)) == 1
    finally:
        storage.dispose()


# --- alertes persistées -------------------------------------------------------


def test_alerts_are_persisted_with_the_incident_lifecycle(tmp_path):
    loop, _, storage = _loop(tmp_path, _config(), scenarios=[_drop()])
    try:
        for k in range(1, 41):
            loop.run_tick(NOW + k * IV)

        incident = next(
            i for i in storage.list_incidents(limit=50) if i.output_id == "OUT_001" and i.opened_at
        )
        alerts = storage.list_alerts(output_id="OUT_001", limit=500)
        events = [a.event for a in reversed(alerts) if a.incident_id == incident.id]

        assert events[0] == "suspect"
        assert "opened" in events and events[-1] == "closed"
        assert events.index("opened") < events.index("closed")
        assert all(a.output_id == "OUT_001" for a in alerts)

        # Les autres sorties peuvent avoir des signaux faibles (suspect puis résorbé),
        # mais aucun incident confirmé : pas d'alerte "opened".
        assert all(a.event != "opened" for a in storage.list_alerts(output_id="OUT_002", limit=200))
    finally:
        storage.dispose()
