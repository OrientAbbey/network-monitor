"""Fenêtres de maintenance et dépendances amont/aval : retenir sans jamais cacher, libérer à temps."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from network_monitor.app.config import AppConfig, IncidentConfig
from network_monitor.app.models import AnomalyResult, IncidentStatus, MaintenanceWindow
from network_monitor.app.notify import format_text
from network_monitor.incidents.manager import IncidentManager

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def _manager(parents=None, maintenance=()):
    mgr = IncidentManager(IncidentConfig())
    mgr.set_topology(parents or {}, timedelta(minutes=15))
    windows = list(maintenance)
    mgr.set_maintenance_check(lambda out, at: any(w.covers(out, at) for w in windows))
    return mgr


def _win(start_min, end_min, output=None):
    return MaintenanceWindow(id="w", output_id=output, start=T0 + start_min * M, end=T0 + end_min * M, reason="travaux", created_at=T0)


def _feed(mgr, steps):
    """`steps` : (minute, sortie, score). Renvoie (toutes les alertes, incidents vus par id)."""
    alerts, incidents = [], {}
    for minute, output, score in sorted(steps, key=lambda s: s[0]):
        r = AnomalyResult(
            output_id=output, timestamp=T0 + minute * M, traffic_anomaly_score=score, loss_anomaly_score=0.0,
            joint_anomaly_score=score, traffic_deviation_percent=-60.0 if score > 0.5 else 0.0,
        )
        got, inc = mgr.process(r, expected_traffic_mbps=100.0, observed_traffic_mbps=40.0, observed_loss_percent=0.1)
        alerts += got
        if inc is not None:
            incidents[inc.id] = inc
    return alerts, incidents


def outage(output, start, minutes=40, step=5):
    """Une panne franche de `minutes`, puis retour à la normale (deux pas calmes)."""
    up = [(start + k, output, 0.9) for k in range(0, minutes, step)]
    down = [(start + minutes + k, output, 0.1) for k in (0, step)]
    return up + down


def _by(alerts, event=None, output=None):
    return [a for a in alerts if (event is None or a.event == event) and (output is None or a.output_id == output)]


def _confirmed(incidents):
    return [i for i in incidents.values() if i.opened_at is not None]


# -- maintenance ------------------------------------------------------------------------------------


def test_an_incident_inside_a_maintenance_window_is_recorded_but_never_announced():
    mgr = _manager(maintenance=[_win(-10, 120, "O")])
    alerts, incidents = _feed(mgr, outage("O", 0))
    [incident] = _confirmed(incidents)
    assert incident.maintenance is True and incident.status == IncidentStatus.CLOSED  # il existe, il est dans l'historique
    notable = [a for a in alerts if a.confirmed]
    assert {a.event for a in notable} >= {"opened", "closed"}
    assert all(a.suppressed == "maintenance" for a in notable)  # mais aucune n'est envoyée


def test_another_output_is_not_silenced_by_a_window():
    mgr = _manager(maintenance=[_win(-10, 120, "OTHER")])
    alerts, incidents = _feed(mgr, outage("O", 0))
    assert [i.maintenance for i in _confirmed(incidents)] == [False]
    assert all(a.suppressed is None for a in alerts)
    wide = _manager(maintenance=[_win(-10, 120, None)])  # None = toutes les sorties
    assert all(a.suppressed == "maintenance" for a in _feed(wide, outage("O", 0))[0] if a.confirmed)


def test_an_incident_that_outlives_the_maintenance_becomes_a_real_alert():
    """Maintenance de 0 à 25 min, panne qui dure jusqu'à 60 min : la fin de la fenêtre libère l'alerte."""
    mgr = _manager(maintenance=[_win(-5, 25, "O")])
    alerts, _ = _feed(mgr, outage("O", 0, minutes=60))
    notable = [a for a in alerts if a.confirmed]
    held = [a for a in notable if a.suppressed == "maintenance"]
    released = [a for a in notable if a.suppressed is None]
    assert held and released
    first_real = min(released, key=lambda a: a.timestamp)
    assert first_real.event == "opened" and "après la fin de la maintenance" in first_real.message
    assert first_real.timestamp >= T0 + 25 * M  # jamais avant la fin de la fenêtre
    assert _by(alerts, "closed", "O")[-1].suppressed is None  # la résolution d'un incident annoncé est annoncée


def test_an_incident_that_ends_inside_the_window_never_announces_its_closure_either():
    mgr = _manager(maintenance=[_win(-5, 200, "O")])
    alerts, _ = _feed(mgr, outage("O", 0, minutes=30))
    assert all(a.suppressed == "maintenance" for a in alerts if a.confirmed)  # jamais de « résolu » pour un incident jamais annoncé


# -- dépendances -------------------------------------------------------------------------------------


def test_downstream_incident_is_held_when_its_upstream_is_already_open():
    mgr = _manager(parents={"B": "A"})
    alerts, incidents = _feed(mgr, outage("A", 0) + outage("B", 5))
    inc_a = next(i for i in _confirmed(incidents) if i.output_id == "A")
    inc_b = next(i for i in _confirmed(incidents) if i.output_id == "B")
    assert inc_b.root_cause_incident_id == inc_a.id and inc_b.inhibited is True and inc_a.root_cause_incident_id is None
    assert all(a.suppressed is None for a in _by(alerts, output="A") if a.confirmed)  # l'amont est annoncé
    assert all(a.suppressed == "inhibited" for a in _by(alerts, output="B") if a.confirmed)  # l'aval, non : une panne, une alerte
    assert all(a.root_cause_incident_id == inc_a.id for a in _by(alerts, output="B") if a.confirmed)


def test_upstream_that_opens_second_names_the_outputs_it_already_explains():
    mgr = _manager(parents={"B": "A"})
    alerts, incidents = _feed(mgr, outage("B", 0) + outage("A", 5))  # l'aval a été confirmé le premier
    inc_a = next(i for i in _confirmed(incidents) if i.output_id == "A")
    inc_b = next(i for i in _confirmed(incidents) if i.output_id == "B")
    assert inc_b.root_cause_incident_id == inc_a.id  # rattaché après coup
    assert inc_b.inhibited is False and _by(alerts, "opened", "B")[0].suppressed is None  # déjà annoncé : on ne le retient pas rétroactivement
    assert _by(alerts, "opened", "A")[0].impacted_outputs == ["B"]  # l'amont dit qui d'autre il touche


def test_dependency_is_transitive():
    mgr = _manager(parents={"B": "A", "C": "B"})
    _, incidents = _feed(mgr, outage("A", 0) + outage("C", 10))  # B n'est pas en incident : C remonte quand même jusqu'à A
    inc_a = next(i for i in _confirmed(incidents) if i.output_id == "A")
    inc_c = next(i for i in _confirmed(incidents) if i.output_id == "C")
    assert inc_c.root_cause_incident_id == inc_a.id and inc_c.inhibited


def test_unrelated_or_far_apart_incidents_are_independent():
    mgr = _manager(parents={"B": "A"})
    alerts, incidents = _feed(mgr, outage("A", 0, minutes=120) + outage("B", 60))  # 60 min d'écart > 15 min de fenêtre
    assert all(i.root_cause_incident_id is None and not i.inhibited for i in _confirmed(incidents))
    assert all(a.suppressed is None for a in alerts)
    independent = _manager(parents={})
    assert all(a.suppressed is None for a in _feed(independent, outage("A", 0) + outage("B", 5))[0])


def test_a_held_downstream_incident_is_released_when_the_cause_is_resolved_but_it_persists():
    mgr = _manager(parents={"B": "A"})
    # A : 0 → 40 min ; B : démarre à 5 min et dure jusqu'à 100 min (ses propres soucis après la résolution de A)
    alerts, incidents = _feed(mgr, outage("A", 0) + outage("B", 5, minutes=95))
    inc_b = next(i for i in _confirmed(incidents) if i.output_id == "B")
    b_alerts = [a for a in _by(alerts, output="B") if a.confirmed]
    released = [a for a in b_alerts if a.suppressed is None]
    assert any(a.suppressed == "inhibited" for a in b_alerts) and released
    assert released[0].event == "opened" and "résolution de la cause amont" in released[0].message
    assert released[0].timestamp >= T0 + 40 * M  # jamais avant la résolution de l'amont
    assert _by(alerts, "closed", "B")[-1].suppressed is None and inc_b.status == IncidentStatus.CLOSED


def test_a_held_downstream_incident_that_ends_with_its_cause_stays_silent():
    mgr = _manager(parents={"B": "A"})
    alerts, _ = _feed(mgr, outage("A", 0) + outage("B", 5, minutes=35))  # B se termine à 40 min, avec A
    assert all(a.suppressed == "inhibited" for a in _by(alerts, output="B") if a.confirmed)


# -- configuration et notifications ----------------------------------------------------------------------


def test_topology_is_validated():
    with pytest.raises(ValueError, match="propre parent"):
        AppConfig.model_validate({"topology": {"parents": {"A": "A"}}})
    with pytest.raises(ValueError, match="cycle"):
        AppConfig.model_validate({"topology": {"parents": {"A": "B", "B": "C", "C": "A"}}})
    ok = AppConfig.model_validate({"topology": {"parents": {"B": "A", "C": "A"}, "correlation_window": "30m"}})
    assert ok.topology.correlation_window == timedelta(minutes=30)


def test_maintenance_window_validation_and_coverage():
    w = _win(10, 20, "O")
    assert w.covers("O", T0 + 10 * M) and not w.covers("O", T0 + 20 * M) and not w.covers("X", T0 + 15 * M)  # début inclus, fin exclue
    assert _win(10, 20).covers("anything", T0 + 15 * M)
    with pytest.raises(ValueError, match="suivre"):
        _win(20, 10)
    naive = datetime(2026, 1, 1)  # noqa: DTZ001 - naïf volontairement
    with pytest.raises(ValueError, match="fuseau"):
        MaintenanceWindow(id="x", start=naive, end=datetime(2026, 1, 2, tzinfo=timezone.utc), reason="r", created_at=T0)
    cancelled = w.model_copy(update={"cancelled_at": T0})
    assert not cancelled.covers("O", T0 + 15 * M)


def test_suppressed_alerts_are_never_planned_for_sending(tmp_path, monkeypatch):
    import httpx

    from network_monitor.app.notify import NotificationDispatcher
    from network_monitor.storage.repository import SqlAlchemyRepository

    monkeypatch.setenv("NM_TELEGRAM_TOKEN", "t")
    cfg = AppConfig.model_validate({"notifications": {"enabled": True, "channels": [{"name": "tg", "type": "telegram", "chat_id": "-1"}]}})
    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'n.db'}")
    try:
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
        d = NotificationDispatcher(cfg.notifications, repo, client)
        alerts, _ = _feed(_manager(parents={"B": "A"}), outage("A", 0) + outage("B", 5))
        planned = d.plan(alerts)
        assert {n.alert["output_id"] for n in planned} == {"A"}  # l'aval retenu n'est pas planifié
        held = _by(alerts, "opened", "B")[0]
        assert held.suppressed == "inhibited" and not any(n.alert["incident_id"] == held.incident_id for n in planned)
        reverse = d.plan(_feed(_manager(parents={"B": "A"}), outage("B", 0) + outage("A", 5))[0])
        assert any("Explique aussi : B" in format_text([n.alert], ZoneInfo("UTC")) for n in reverse)
    finally:
        repo.dispose()
