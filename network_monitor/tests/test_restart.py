"""Reprise après redémarrage : les incidents actifs enregistrés sont repris, les orphelins fermés."""

from datetime import UTC, datetime, timedelta

from network_monitor.app.config import AppConfig, IncidentConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import (
    AnomalyResult,
    Incident,
    IncidentStatus,
    IncidentType,
    Severity,
)
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.incidents.manager import IncidentManager
from network_monitor.storage.repository import SqlAlchemyRepository

NOW = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
IV = timedelta(minutes=5)


def _incident(iid, output="OUT_001", status=IncidentStatus.OPEN, last_seen_min=60, **kw):
    return Incident(
        id=iid, output_id=output, status=status, severity=Severity.WARNING,
        incident_type=IncidentType.TRAFFIC_DROP,
        suspected_at=NOW + timedelta(minutes=last_seen_min - 60), opened_at=NOW + timedelta(minutes=last_seen_min - 50),
        last_seen_at=NOW + timedelta(minutes=last_seen_min), score=0.9, description="repris", **kw,
    )


def _normal(minute, output="OUT_001"):
    return AnomalyResult(output_id=output, timestamp=NOW + timedelta(minutes=minute),
                         traffic_anomaly_score=0.0, loss_anomaly_score=0.0, joint_anomaly_score=0.0)


# --- IncidentManager.restore --------------------------------------------------


def test_restored_incident_is_tracked_and_closes_after_the_recovery_duration():
    mgr = IncidentManager(IncidentConfig(recovery_duration=timedelta(minutes=10)))
    assert mgr.restore([_incident("a")]) == []
    assert [i.id for i in mgr.active_incidents()] == ["a"]

    closed = None
    for minute in (65, 70, 75, 80):
        alerts, incident = mgr.process(_normal(minute))
        if any(a.event.value == "closed" for a in alerts):
            closed = incident
    assert closed is not None and closed.id == "a" and closed.status == IncidentStatus.CLOSED
    assert mgr.active_incidents() == []


def test_restored_incident_is_not_closed_before_the_recovery_duration():
    mgr = IncidentManager(IncidentConfig(recovery_duration=timedelta(minutes=10)))
    mgr.restore([_incident("a")])
    _, incident = mgr.process(_normal(65))
    assert incident is not None and incident.status != IncidentStatus.CLOSED
    assert mgr.active_incidents()


def test_restore_keeps_the_most_recent_active_incident_per_output_and_closes_orphans():
    older = _incident("old", last_seen_min=30)
    newer = _incident("new", last_seen_min=90)
    mgr = IncidentManager(IncidentConfig())
    superseded = mgr.restore([newer, older])

    assert [i.id for i in superseded] == ["old"]
    assert older.status == IncidentStatus.CLOSED and older.closed_at == older.last_seen_at
    assert [i.id for i in mgr.active_incidents()] == ["new"]


def test_restore_tracks_each_output_independently_and_ignores_closed_incidents():
    closed = _incident("done", output="OUT_003", status=IncidentStatus.CLOSED, closed_at=NOW + timedelta(minutes=60))
    mgr = IncidentManager(IncidentConfig())
    mgr.restore([_incident("a"), _incident("b", output="OUT_002"), closed])
    assert {i.id for i in mgr.active_incidents()} == {"a", "b"}


# --- MonitoringLoop : continuité à travers un redémarrage ----------------------


def _config() -> AppConfig:
    return AppConfig.model_validate({"history": {"minimum": "30d", "preferred": "40d"}})


def _source() -> MockDataSource:
    src = MockDataSource(seed=3)
    src.add_scenario(
        AnomalyScenario(
            scenario_id="LONG", output_id="OUT_001", start_time=NOW + IV, end_time=NOW + timedelta(hours=8),
            type=ScenarioType.LONG_INCIDENT, severity=Severity.CRITICAL,
        )
    )
    return src


def _start(url, at):
    repo = SqlAlchemyRepository(url)
    src = _source()
    loop = MonitoringLoop(src, repo, _config())
    loop.calibrate(at)
    src.start_realtime(at)
    return loop, repo


def test_incident_survives_a_restart_and_is_closed_when_the_anomaly_ends(tmp_path):
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    loop, repo = _start(url, NOW)
    for k in range(1, 31):
        loop.run_tick(NOW + k * IV)
    (before,) = [i for i in repo.list_active_incidents() if i.output_id == "OUT_001"]
    evidence_before = len(before.evidence)
    repo.dispose()

    restart_at = NOW + 30 * IV
    loop2, repo2 = _start(url, restart_at)
    try:
        assert [i.id for i in loop2._incident_manager.active_incidents()] == [before.id]
        for k in range(1, 80):  # l'anomalie se termine à +8 h pendant cette période
            loop2.run_tick(restart_at + k * IV)

        incidents = [i for i in repo2.list_incidents(limit=50) if i.output_id == "OUT_001" and i.opened_at]
        assert [i.id for i in incidents] == [before.id]  # pas de doublon créé par le redémarrage
        assert incidents[0].status == IncidentStatus.CLOSED
        assert len(incidents[0].evidence) > evidence_before  # les preuves d'avant sont conservées, complétées
        assert repo2.list_active_incidents() == []
    finally:
        repo2.dispose()


def test_orphans_from_previous_restarts_are_closed_on_startup(tmp_path):
    url = f"sqlite:///{tmp_path / 'orphans.db'}"
    seed = SqlAlchemyRepository(url)
    seed.upsert_incident(_incident("orphan-1", last_seen_min=-300))
    seed.upsert_incident(_incident("orphan-2", last_seen_min=-200))
    seed.upsert_incident(_incident("current", last_seen_min=-10))
    seed.dispose()

    _loop, repo = _start(url, NOW)
    try:
        assert [i.id for i in repo.list_active_incidents()] == ["current"]
        for orphan_id in ("orphan-1", "orphan-2"):
            orphan = repo.get_incident(orphan_id)
            assert orphan is not None
            assert orphan.status == IncidentStatus.CLOSED and orphan.closed_at == orphan.last_seen_at
    finally:
        repo.dispose()


def test_recalibration_does_not_reload_or_reset_the_incident_state(tmp_path):
    url = f"sqlite:///{tmp_path / 'recal.db'}"
    seed = SqlAlchemyRepository(url)
    seed.upsert_incident(_incident("current", last_seen_min=-10))
    seed.dispose()

    loop, repo = _start(url, NOW)
    try:
        (tracked,) = loop._incident_manager.active_incidents()
        tracked.score = 0.5  # état en mémoire, pas encore persisté
        loop.calibrate(NOW + timedelta(days=1))
        (still,) = loop._incident_manager.active_incidents()
        assert still is tracked and still.score == 0.5
    finally:
        repo.dispose()
