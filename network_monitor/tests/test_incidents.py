"""Tests du moteur d'incidents : hystérésis, persistance, trous de données (§34)."""

from datetime import datetime, timedelta, timezone

from network_monitor.app.config import IncidentConfig
from network_monitor.app.models import AnomalyResult, IncidentStatus
from network_monitor.incidents.manager import IncidentManager

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc)


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
    # Pas de "updated" : la sévérité n'a pas changé entre 0.85 et 0.9 (§27, anti-spam).
    assert events == ["suspect", "opened", "recovering", "closed"]
    assert mgr.active_incidents() == []


def test_open_incident_does_not_emit_an_alert_per_tick():
    """Un incident OPEN stable ne doit pas générer une alerte à chaque mesure (§27)."""
    mgr = _manager()
    events = []
    for m in range(0, 120, 5):
        events += [a.event for a in mgr.process(_result(m, 0.9))[0]]
    assert events.count("opened") == 1
    assert len(events) <= 3


def test_confirmed_new_episode_within_merge_window_reopens_same_incident():
    """§22 : un incident intermittent reste un seul incident, pas une suite d'incidents."""
    mgr = _manager()
    first = None
    for m, s in [(0, 0.6), (10, 0.85), (20, 0.9), (25, 0.1), (35, 0.1)]:
        _, inc = mgr.process(_result(m, s))
        first = first or inc
    assert first is not None and first.opened_at is not None and mgr.active_incidents() == []  # fermé
    events, incident = [], None
    for m in (40, 45, 50, 55):  # nouvel épisode, dans merge_window (20 min) après la fin
        alerts, incident = mgr.process(_result(m, 0.9))
        events += [a.event for a in alerts]
    assert incident is not None and incident.id == first.id  # même incident, pas un nouveau
    assert incident.status == IncidentStatus.OPEN and "reopened" in events
    assert incident.started_at == first.started_at  # début d'origine conservé
    assert incident.ended_at is None  # il dure de nouveau


def test_noise_right_after_an_incident_does_not_extend_it():
    """Un pic isolé juste après la fin d'un incident n'est pas un nouvel épisode confirmé."""
    mgr = _manager()
    for m, s in [(0, 0.6), (10, 0.85), (20, 0.9), (25, 0.1), (35, 0.1)]:
        _, inc = mgr.process(_result(m, s))
    ended = inc.ended_at
    _, spike = mgr.process(_result(40, 0.9))
    assert spike is not None and spike.status == IncidentStatus.SUSPECT and spike.opened_at is None
    for m in (45, 50):
        _, spike = mgr.process(_result(m, 0.1))
    assert mgr.active_incidents() == []
    assert inc.status == IncidentStatus.CLOSED and inc.ended_at == ended  # l'incident d'origine n'a pas bougé


def test_incident_dates_are_retro_dated():
    """Début = premier signal faible ; fin = premier pas de retour à la normale (pas la décision de fermer)."""
    mgr = _manager()
    for m, s in [(0, 0.6), (10, 0.85), (20, 0.9), (25, 0.3), (35, 0.1)]:
        _, incident = mgr.process(_result(m, s))
    assert incident is not None and incident.status == IncidentStatus.CLOSED
    t0 = _result(0, 0).timestamp
    assert incident.started_at == t0 and incident.suspected_at == t0
    assert incident.opened_at == _result(10, 0).timestamp  # confirmé 10 min plus tard
    assert incident.ended_at == _result(25, 0).timestamp  # premier pas sous close_score
    assert incident.closed_at == _result(35, 0).timestamp  # fermeture décidée après recovery_duration
    assert incident.duration_seconds == 25 * 60  # 0 → 25 min : durée réelle, pas 10 → 35


def test_unconfirmed_suspect_is_not_worth_persisting():
    """Un signal faible résorbé sans jamais ouvrir n'est que du bruit (§19)."""
    from network_monitor.incidents.manager import incident_worth_persisting

    mgr = _manager()
    _, incident = mgr.process(_result(0, 0.6))
    assert not incident_worth_persisting(incident)
    _, incident = mgr.process(_result(5, 0.1))
    assert incident.status == IncidentStatus.CLOSED
    assert not incident_worth_persisting(incident)
    for m, s in [(20, 0.6), (30, 0.85)]:
        _, incident = mgr.process(_result(m, s))
    assert incident_worth_persisting(incident)  # confirmé (opened_at renseigné)


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
