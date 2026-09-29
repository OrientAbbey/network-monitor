"""Tests du moteur d'incidents : hystérésis, persistance, trous de données (§34)."""

from datetime import UTC, datetime, timedelta

from network_monitor.app.config import IncidentConfig
from network_monitor.app.models import AnomalyResult, IncidentStatus
from network_monitor.incidents.manager import IncidentManager

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)


def _result(minute, score, dg=False, output_id="OUT_017"):
    return AnomalyResult(
        output_id=output_id,
        timestamp=T0 + timedelta(minutes=minute),
        traffic_anomaly_score=score,
        loss_anomaly_score=score * 0.9,
        joint_anomaly_score=score,
        traffic_deviation_percent=-50.0,
        data_gap=dg,
    )


def _manager(**overrides) -> IncidentManager:
    cfg = IncidentConfig(
        minimum_duration=timedelta(minutes=10),
        recovery_duration=timedelta(minutes=10),
        stale_after=timedelta(minutes=15),
        **overrides,
    )
    return IncidentManager(cfg, critical_score=0.80)


def test_isolated_anomaly_does_not_open_incident():
    """Une anomalie isolée ne doit pas immédiatement créer un incident (§20)."""
    mgr = _manager()
    events = []
    for m, s in [(0, 0.1), (5, 0.85), (10, 0.1), (15, 0.1)]:
        events += [a.event for a in mgr.process(_result(m, s))[0]]
    assert "opened" not in events
    assert mgr.active_incidents() == []


def test_persistent_anomaly_opens_incident_after_minimum_duration():
    """Anomalie persistante >= open_score pendant minimum_duration -> ouverture (§20)."""
    mgr = _manager()
    events = []
    for m, s in [(0, 0.6), (5, 0.8), (10, 0.85), (15, 0.9)]:
        events += [a.event for a in mgr.process(_result(m, s))[0]]
    assert "opened" in events
    assert mgr.active_incidents()[0].status == IncidentStatus.OPEN


def test_hysteresis_prevents_flapping_around_threshold():
    """Un score oscillant entre close_score et open_score ne doit pas fermer l'incident (§21)."""
    mgr = _manager()
    for m, s in [(0, 0.6), (10, 0.85), (20, 0.5), (30, 0.85), (40, 0.5)]:
        mgr.process(_result(m, s))
    assert mgr.active_incidents()[0].status == IncidentStatus.OPEN


def test_full_lifecycle_suspect_open_recovering_closed():
    """Cycle complet incluant la durée de récupération (§22)."""
    mgr = _manager()
    events = []
    for m, s in [(0, 0.6), (10, 0.85), (20, 0.9), (25, 0.3), (35, 0.1)]:
        events += [a.event for a in mgr.process(_result(m, s))[0]]
    assert events == ["suspect", "opened", "updated", "recovering", "closed"]
    assert mgr.active_incidents() == []


def test_missing_data_suspends_but_does_not_reset_persistence():
    """Une donnée MISSING pendant un incident OPEN ne doit ni le fermer ni le faire progresser (§31)."""
    mgr = _manager()
    for m, s in [(0, 0.6), (10, 0.85)]:
        mgr.process(_result(m, s))
    assert mgr.active_incidents()[0].status == IncidentStatus.OPEN

    # Deux trous de données consécutifs, sous le seuil stale_after (15 min).
    mgr.process(_result(15, 0.0, dg=True))
    mgr.process(_result(20, 0.0, dg=True))
    assert mgr.active_incidents()[0].status == IncidentStatus.OPEN


def test_prolonged_gap_marks_incident_stale_then_recovers_on_valid_data():
    """Au-delà de stale_after, l'incident passe STALE puis redevient OPEN à la reprise (§31)."""
    mgr = _manager()
    for m, s in [(0, 0.6), (10, 0.85)]:
        mgr.process(_result(m, s))

    for m in (15, 20, 25, 30):  # trou de 20 min > stale_after (15 min)
        mgr.process(_result(m, 0.0, dg=True))
    assert mgr.active_incidents()[0].status == IncidentStatus.STALE

    mgr.process(_result(35, 0.85))
    assert mgr.active_incidents()[0].status == IncidentStatus.OPEN


def test_per_output_override_changes_minimum_duration():
    """Les durées d'hystérésis sont surchargeables par sortie (§21)."""
    from network_monitor.app.config import IncidentOutputOverride

    mgr = _manager(per_output_overrides={"OUT_017": IncidentOutputOverride(minimum_duration=timedelta(minutes=20))})
    events = []
    for m, s in [(0, 0.6), (10, 0.85)]:  # seulement 10 min : insuffisant avec l'override à 20 min
        events += [a.event for a in mgr.process(_result(m, s))[0]]
    assert "opened" not in events


def test_multiple_outputs_are_tracked_independently():
    mgr = _manager()
    for m, s in [(0, 0.6), (10, 0.85)]:
        mgr.process(_result(m, s, output_id="OUT_001"))
    mgr.process(_result(0, 0.1, output_id="OUT_002"))
    active = {i.output_id: i.status for i in mgr.active_incidents()}
    assert active == {"OUT_001": IncidentStatus.OPEN}
