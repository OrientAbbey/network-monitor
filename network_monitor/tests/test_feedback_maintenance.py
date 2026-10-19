"""Retour « faux positif », fenêtres de maintenance et dépendances : stockage, API, boucle de bout en bout."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.analysis.availability import SampleStats, build_report
from network_monitor.app.config import AppConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import (
    Incident,
    IncidentStatus,
    IncidentType,
    IncidentVerdict,
    MaintenanceWindow,
    Severity,
    TrafficSample,
)
from network_monitor.app.notify import NotificationDispatcher
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.incidents.manager import Alert
from network_monitor.storage.repository import SqlAlchemyRepository

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
H = timedelta(hours=1)
STEP = timedelta(minutes=5)
AUTH = {"Authorization": "Bearer tok"}


def _inc(ident="i1", output="OUT_001", start=2.0, end=3.0, severity=Severity.CRITICAL, itype=IncidentType.TRAFFIC_DROP, confirmed=True):
    return Incident(
        id=ident, output_id=output, status=IncidentStatus.CLOSED, severity=severity, incident_type=itype,
        suspected_at=T0 + start * H, opened_at=T0 + (start + 0.25) * H if confirmed else None,
        started_at=T0 + start * H if confirmed else None, ended_at=T0 + end * H if confirmed else None,
        closed_at=T0 + (end + 0.1) * H, last_seen_at=T0 + end * H, score=0.9,
    )


@pytest.fixture
def repo(tmp_path):
    r = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'f.db'}")
    yield r
    r.dispose()


# -- stockage ---------------------------------------------------------------------------------------------


def test_feedback_is_stored_apart_and_survives_the_engine_rewriting_the_incident(repo):
    repo.upsert_incident(_inc())
    repo.set_feedback("i1", IncidentVerdict.FALSE_POSITIVE, "alice", T0 + 5 * H, "bruit du collecteur")
    got = repo.get_incident("i1")
    assert got is not None and got.verdict == IncidentVerdict.FALSE_POSITIVE and got.verdict_by == "alice"
    assert got.verdict_note == "bruit du collecteur" and got.verdict_at == T0 + 5 * H
    repo.upsert_incident(_inc())  # le moteur réécrit la ligne de l'incident à chaque pas : le verdict doit rester
    again = repo.get_incident("i1")
    assert again is not None and again.verdict == IncidentVerdict.FALSE_POSITIVE
    assert [i.verdict for i in repo.list_incidents(limit=10)] == [IncidentVerdict.FALSE_POSITIVE]
    assert [i.verdict for i in repo.list_incidents_between(T0, T0 + 10 * H)] == [IncidentVerdict.FALSE_POSITIVE]
    repo.set_feedback("i1", IncidentVerdict.TRUE_POSITIVE, "bob", T0 + 6 * H)  # un second avis remplace le premier
    second = repo.get_incident("i1")
    assert second is not None and second.verdict_by == "bob"
    repo.set_feedback("i1", None, "bob", T0 + 7 * H)  # `None` retire le jugement
    cleared = repo.get_incident("i1")
    assert cleared is not None and cleared.verdict is None


def test_incident_dependency_fields_roundtrip(repo):
    inc = _inc().model_copy(update={"maintenance": True, "inhibited": True, "root_cause_incident_id": "root"})
    repo.upsert_incident(inc)
    got = repo.get_incident("i1")
    assert got is not None and (got.maintenance, got.inhibited, got.root_cause_incident_id) == (True, True, "root")


def test_alert_suppression_fields_roundtrip(repo):
    alert = Alert(
        incident_id="i1", output_id="OUT_001", severity=Severity.CRITICAL, event="opened", timestamp=T0, message="m",
        incident_type=IncidentType.TRAFFIC_DROP, suppressed="inhibited", root_cause_incident_id="root", impacted_outputs=["B", "C"],
    )
    repo.save_alerts([alert])
    [got] = repo.list_alerts()
    assert (got.suppressed, got.root_cause_incident_id, got.impacted_outputs) == ("inhibited", "root", ["B", "C"])


def test_maintenance_windows_overlap_queries_and_cancellation(repo):
    def w(ident, start, end, output="OUT_001"):
        return MaintenanceWindow(id=ident, output_id=output, start=T0 + start * H, end=T0 + end * H, reason="travaux", created_by="alice", created_at=T0)

    repo.add_maintenance(w("a", 1, 3))
    repo.add_maintenance(w("b", 5, 6, None))
    assert [x.id for x in repo.list_maintenance(T0 + 2 * H, T0 + 5 * H)] == ["a"]  # b commence à 5 h : pas de chevauchement
    assert [x.id for x in repo.list_maintenance(T0, T0 + 10 * H)] == ["a", "b"]
    assert [x.id for x in repo.list_maintenance(T0 + 3 * H, T0 + 4 * H)] == []  # a se termine à 3 h (fin exclue)
    assert repo.cancel_maintenance("a", T0 + 2 * H) is True and repo.cancel_maintenance("a", T0 + 2 * H) is False
    assert repo.cancel_maintenance("nope", T0) is False
    assert [x.id for x in repo.list_maintenance(T0, T0 + 10 * H)] == ["b"]
    assert {x.id for x in repo.list_maintenance(T0, T0 + 10 * H, include_cancelled=True)} == {"a", "b"}


# -- rapport de disponibilité ---------------------------------------------------------------------------------------


def _report(incidents, maintenance=None, hours=24, **kw):
    stats = {"O": SampleStats(hours * 12 + 1, T0)}
    return build_report(incidents, ["O"], T0, T0 + hours * H, T0 + hours * H, STEP, stats, maintenance=maintenance, **kw).outputs[0]


def test_report_excludes_false_positives_unless_asked():
    fp = _inc(output="O", start=2, end=4).model_copy(update={"verdict": IncidentVerdict.FALSE_POSITIVE})
    real = _inc(ident="i2", output="O", start=10, end=11)
    r = _report([fp, real])
    assert r.incidents == 1 and r.downtime_seconds == 3600 and r.false_positives_excluded == 1
    assert _report([fp, real], include_false_positives=True).downtime_seconds == 3 * 3600


def test_report_subtracts_planned_maintenance_from_downtime_and_from_due_time():
    window = MaintenanceWindow(id="w", output_id="O", start=T0 + 2 * H, end=T0 + 4 * H, reason="r", created_at=T0)
    r = _report([_inc(output="O", start=1, end=5)], maintenance=[window])  # panne de 1 h à 5 h, dont 2 h planifiées (2 h → 4 h)
    assert r.downtime_seconds == 2 * 3600 and r.maintenance_seconds == 2 * 3600
    assert r.availability_percent == pytest.approx(100 * (1 - 2 / 22), abs=1e-3)  # 2 h sur 22 h dues, pas sur 24 h
    other = MaintenanceWindow(id="x", output_id="OTHER", start=T0 + 2 * H, end=T0 + 4 * H, reason="r", created_at=T0)
    assert _report([_inc(output="O", start=1, end=5)], maintenance=[other]).maintenance_seconds == 0
    cancelled = window.model_copy(update={"cancelled_at": T0})
    assert _report([_inc(output="O", start=1, end=5)], maintenance=[cancelled]).downtime_seconds == 4 * 3600
    everything = MaintenanceWindow(id="all", output_id=None, start=T0, end=T0 + 24 * H, reason="r", created_at=T0)
    full = _report([_inc(output="O", start=1, end=5)], maintenance=[everything])
    assert full.availability_percent is None and full.downtime_seconds == 0  # tout le temps était planifié : rien n'est dû


# -- API ---------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NM_API_TOKEN", "tok")
    state = api_module.MonitoringState(
        config=AppConfig.model_validate({"history": {"minimum": "30d", "preferred": "40d"}}),
        db_url=f"sqlite:///{tmp_path / 'a.db'}",
    )
    state.now = T0 + 24 * H
    api_module.state = state
    state.storage.save_samples([
        TrafficSample(timestamp=T0 + i * STEP, output_id=o, traffic_mbps=100.0, loss_percent=0.1)
        for o in ("OUT_001", "OUT_002", "OUT_003") for i in range(24 * 12 + 1)
    ])
    with TestClient(api_module.app) as c:
        state.stop()
        yield c
    state.storage.dispose()


def test_feedback_endpoint_requires_login_and_validates_the_incident(client):
    api_module.state.storage.upsert_incident(_inc(output="OUT_001"))
    api_module.state.storage.upsert_incident(_inc(ident="noise", confirmed=False).model_copy(update={"opened_at": None}))
    body = {"verdict": "false_positive", "note": "collecteur en panne"}
    assert client.put("/incidents/i1/feedback", json=body).status_code == 401
    assert client.put("/incidents/nope/feedback", json=body, headers=AUTH).status_code == 404
    assert client.put("/incidents/noise/feedback", json=body, headers=AUTH).status_code == 409  # jamais confirmé
    assert client.put("/incidents/i1/feedback", json={"verdict": "maybe"}, headers=AUTH).status_code == 422
    r = client.put("/incidents/i1/feedback", json=body, headers=AUTH)
    assert r.status_code == 200 and r.json()["verdict"] == "false_positive" and r.json()["verdict_by"] == "api-token"
    assert client.get("/incidents/i1").json()["verdict_note"] == "collecteur en panne"
    cleared = client.put("/incidents/i1/feedback", json={"verdict": None}, headers=AUTH)
    assert cleared.json()["verdict"] is None


def test_precision_report_counts_only_judged_incidents(client):
    s = api_module.state.storage
    for i, (out, verdict, itype) in enumerate([
        ("OUT_001", IncidentVerdict.TRUE_POSITIVE, IncidentType.TRAFFIC_DROP),
        ("OUT_001", IncidentVerdict.TRUE_POSITIVE, IncidentType.HIGH_LOSS),
        ("OUT_002", IncidentVerdict.FALSE_POSITIVE, IncidentType.TRAFFIC_DROP),
        ("OUT_002", None, IncidentType.TRAFFIC_DROP),  # sans verdict : ne compte pas dans la précision
    ]):
        s.upsert_incident(_inc(ident=f"p{i}", output=out, start=1 + i * 2, end=2 + i * 2, itype=itype))
        if verdict:
            s.set_feedback(f"p{i}", verdict, "alice", T0 + 10 * H)
    r = client.get("/reports/precision", params={"days": 1}).json()
    assert (r["confirmed"], r["true_positives"], r["false_positives"], r["unjudged"]) == (4, 2, 1, 1)
    assert r["precision_percent"] == 66.7 and r["judged_share_percent"] == 75.0  # la précision vaut ce que vaut la part jugée
    assert r["by_output"]["OUT_001"]["precision_percent"] == 100.0 and r["by_output"]["OUT_002"]["precision_percent"] == 0.0
    assert r["by_type"]["high_loss"]["true_positives"] == 1
    assert client.get("/reports/precision", params={"days": 1, "output_id": "OUT_002"}).json()["confirmed"] == 2
    empty = client.get("/reports/precision", params={"days": 0.001}).json()
    assert empty["confirmed"] == 0 and empty["precision_percent"] is None  # aucune donnée : pas de pourcentage inventé
    assert client.get("/reports/precision", params={"output_id": "NOPE"}).status_code == 404


def test_availability_endpoint_honours_verdicts_and_maintenance(client):
    s = api_module.state.storage
    s.upsert_incident(_inc(ident="x", output="OUT_001", start=2, end=4))
    s.add_maintenance(MaintenanceWindow(id="w", output_id="OUT_001", start=T0 + 3 * H, end=T0 + 5 * H, reason="r", created_at=T0))

    def row(**q):
        outputs = client.get("/reports/availability", params={"days": 1} | q).json()["outputs"]
        return {o["output_id"]: o for o in outputs}

    first = row()["OUT_001"]
    assert first["downtime_seconds"] == 3600 and first["maintenance_seconds"] == 2 * 3600  # 2 h → 4 h moins 3 h → 5 h
    s.set_feedback("x", IncidentVerdict.FALSE_POSITIVE, "alice", T0 + 10 * H)
    judged = row()["OUT_001"]
    assert judged["downtime_seconds"] == 0 and judged["false_positives_excluded"] == 1
    assert row(include_false_positives="true")["OUT_001"]["downtime_seconds"] == 3600


def test_maintenance_endpoints(client):
    assert client.post("/maintenance", json={"reason": "r", "duration_minutes": 30}).status_code == 401
    ok = client.post("/maintenance", json={"output_id": "OUT_001", "reason": "remplacement carte", "starts_in_minutes": 0, "duration_minutes": 90}, headers=AUTH)
    assert ok.status_code == 201 and ok.json()["state"] == "active" and ok.json()["created_by"] == "api-token"
    start = datetime.fromisoformat(ok.json()["start"].replace("Z", "+00:00"))
    assert start == T0 + 24 * H  # relatif à l'horloge du serveur, pas à celle du client
    soon = client.post("/maintenance", json={"reason": "mise à jour", "starts_in_minutes": 60, "duration_minutes": 30}, headers=AUTH)
    assert soon.json()["state"] == "upcoming" and soon.json()["output_id"] is None
    absolute = client.post("/maintenance", json={"reason": "r", "start": (T0 + 30 * H).isoformat(), "end": (T0 + 31 * H).isoformat()}, headers=AUTH)
    assert absolute.status_code == 201

    def bad(body):
        return client.post("/maintenance", json={"reason": "r"} | body, headers=AUTH).status_code

    assert bad({}) == 422  # ni l'un ni l'autre
    assert bad({"start": T0.isoformat(), "end": (T0 + H).isoformat(), "duration_minutes": 10}) == 422  # les deux à la fois
    assert bad({"start": "2027-01-01T00:00:00", "end": "2027-01-01T01:00:00"}) == 422  # sans fuseau
    assert bad({"start": (T0 + 40 * H).isoformat(), "end": (T0 + 39 * H).isoformat()}) == 422  # fin avant début
    assert bad({"duration_minutes": 60 * 24 * 15}) == 422  # plus de 14 jours
    assert bad({"start": T0.isoformat(), "end": (T0 + H).isoformat()}) == 422  # déjà terminée
    assert bad({"duration_minutes": 30, "output_id": "NOPE"}) == 404

    listed = {w["id"]: w["state"] for w in client.get("/maintenance").json()}
    assert sorted(listed.values()) == ["active", "upcoming", "upcoming"]
    assert client.delete(f"/maintenance/{ok.json()['id']}").status_code == 401
    cancelled = client.delete(f"/maintenance/{ok.json()['id']}", headers=AUTH)
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert client.delete(f"/maintenance/{ok.json()['id']}", headers=AUTH).status_code == 404  # déjà annulée


def test_alerts_endpoint_exposes_why_an_alert_was_held(client):
    api_module.state.storage.save_alerts([
        Alert(incident_id="i", output_id="OUT_002", severity=Severity.CRITICAL, event="opened", timestamp=T0, message="m",
              incident_type=IncidentType.TRAFFIC_DROP, suppressed="inhibited", root_cause_incident_id="root"),
    ])
    [a] = client.get("/alerts").json()
    assert a["suppressed"] == "inhibited" and a["root_cause_incident_id"] == "root" and a["impacted_outputs"] == []


# -- de bout en bout : la boucle réelle ----------------------------------------------------------------------------------


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
TG = {"name": "tg", "type": "telegram", "chat_id": "-1"}


def _run(tmp_path, monkeypatch, scenarios, topology=None, prepare=None, ticks=70, at_tick=None):
    """`at_tick` : {numéro de pas: fonction(repo)} exécutée juste avant ce pas (un opérateur qui agit en cours de route)."""
    monkeypatch.setenv("NM_TELEGRAM_TOKEN", "t")
    cfg = AppConfig.model_validate({
        "history": {"minimum": "7d", "preferred": "10d"},
        "notifications": {"enabled": True, "channels": [TG], "batch_window": "0s"},
        "topology": topology or {},
    })
    source = MockDataSource(seed=1)
    for sc in scenarios:
        source.add_scenario(sc)
    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'loop.db'}")
    sent: list[str] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    clock = [NOW]
    dispatcher = NotificationDispatcher(cfg.notifications, repo, httpx.Client(transport=httpx.MockTransport(telegram)), lambda: clock[0])
    loop = MonitoringLoop(source, repo, cfg, dispatcher)
    loop.calibrate(NOW)
    source.start_realtime(NOW)
    if prepare:
        prepare(repo)
    for k in range(1, ticks):
        if at_tick and k in at_tick:
            at_tick[k](repo)
        loop.run_tick(NOW + k * STEP)
        clock[0] = NOW + k * STEP
        dispatcher.run_once()
    return repo, sent, loop


def _drop(output, start_tick, end_tick):
    return AnomalyScenario(
        scenario_id=f"S-{output}", output_id=output, start_time=NOW + start_tick * STEP, end_time=NOW + end_tick * STEP,
        type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL, traffic_drop_fraction=0.7,
    )


def test_an_incident_during_maintenance_is_recorded_but_nothing_is_sent(tmp_path, monkeypatch):
    window = MaintenanceWindow(id="w", output_id="OUT_001", start=NOW, end=NOW + 20 * H, reason="travaux", created_by="alice", created_at=NOW)
    repo, sent, _ = _run(tmp_path, monkeypatch, [_drop("OUT_001", 6, 40)], prepare=lambda r: r.add_maintenance(window))
    try:
        incident = next(i for i in repo.list_incidents(limit=50) if i.opened_at)
        assert incident.maintenance is True and incident.status == IncidentStatus.CLOSED  # l'incident existe dans l'historique
        notable = [a for a in repo.list_alerts(limit=100) if a.confirmed]
        assert notable and all(a.suppressed == "maintenance" for a in notable)
        assert sent == []  # mais personne n'a été réveillé
    finally:
        repo.dispose()


def test_the_same_incident_without_maintenance_is_announced_and_resolved(tmp_path, monkeypatch):
    repo, sent, _ = _run(tmp_path, monkeypatch, [_drop("OUT_001", 6, 40)])
    try:
        assert any(t.startswith("🔴 INCIDENT — OUT_001") for t in sent) and any(t.startswith("🟢 RÉSOLU — OUT_001") for t in sent)
    finally:
        repo.dispose()


def test_cancelling_the_window_makes_a_running_incident_announced_at_the_next_tick(tmp_path, monkeypatch):
    window = MaintenanceWindow(id="w", output_id="OUT_001", start=NOW, end=NOW + 20 * H, reason="travaux", created_by="alice", created_at=NOW)
    repo, sent, _ = _run(
        tmp_path, monkeypatch, [_drop("OUT_001", 6, 200)], prepare=lambda r: r.add_maintenance(window), ticks=45,
        at_tick={30: lambda r: r.cancel_maintenance("w", NOW + 30 * STEP)},  # l'opérateur annule la fenêtre au pas 30
    )
    try:
        confirmed = [a for a in repo.list_alerts(limit=200) if a.confirmed]
        held = [a for a in confirmed if a.suppressed == "maintenance"]
        released = [a for a in confirmed if a.suppressed is None]
        assert held and all(a.timestamp < NOW + 29 * STEP for a in held)  # retenu tant que la fenêtre existait
        # Le pas 30 est le premier exécuté après l'annulation ; il traite la mesure du pas 29 (le Mock rend, à chaque
        # pas, les mesures de l'intervalle précédent) : c'est donc la première alerte annoncée, et il n'y en a pas avant.
        assert released and min(a.timestamp for a in released) == NOW + 29 * STEP
        assert len([t for t in sent if "INCIDENT — OUT_001" in t]) == 1  # une seule fois
    finally:
        repo.dispose()


def test_a_window_that_ends_during_the_outage_releases_a_real_alert(tmp_path, monkeypatch):
    window = MaintenanceWindow(id="w", output_id="OUT_001", start=NOW, end=NOW + 25 * STEP, reason="travaux", created_by="alice", created_at=NOW)
    repo, sent, _ = _run(tmp_path, monkeypatch, [_drop("OUT_001", 6, 60)], prepare=lambda r: r.add_maintenance(window), ticks=75)
    try:
        assert any("INCIDENT — OUT_001" in t for t in sent)  # annoncé une fois la maintenance terminée
        opened = [a for a in repo.list_alerts(limit=200) if a.event == "opened" and a.confirmed and a.suppressed is None]
        assert opened and min(a.timestamp for a in opened) >= NOW + 25 * STEP  # jamais pendant la fenêtre
        assert any("RÉSOLU — OUT_001" in t for t in sent)
    finally:
        repo.dispose()


def test_upstream_outage_sends_one_message_not_two_and_downstream_is_linked(tmp_path, monkeypatch):
    topo = {"parents": {"OUT_002": "OUT_001"}}
    repo, sent, _ = _run(tmp_path, monkeypatch, [_drop("OUT_001", 6, 40), _drop("OUT_002", 8, 40)], topology=topo)
    try:
        incidents = {i.output_id: i for i in repo.list_incidents(limit=50) if i.opened_at}
        assert incidents["OUT_002"].root_cause_incident_id == incidents["OUT_001"].id and incidents["OUT_002"].inhibited is True
        assert incidents["OUT_001"].root_cause_incident_id is None
        incident_msgs = [t for t in sent if "INCIDENT" in t]
        assert len(incident_msgs) == 1 and "OUT_001" in incident_msgs[0]  # une panne, un message
        assert not any("OUT_002" in t for t in sent)  # l'aval, retenu, n'a rien envoyé (ni ouverture, ni résolution)
        held = [a for a in repo.list_alerts(output_id="OUT_002", limit=100) if a.confirmed]
        assert held and all(a.suppressed == "inhibited" for a in held)  # retenu, jamais caché : visible dans l'historique
    finally:
        repo.dispose()


def test_without_topology_both_outages_are_announced(tmp_path, monkeypatch):
    repo, sent, _ = _run(tmp_path, monkeypatch, [_drop("OUT_001", 6, 40), _drop("OUT_002", 8, 40)])
    try:
        assert len([t for t in sent if "INCIDENT" in t]) == 2
    finally:
        repo.dispose()


def test_recalibration_ignores_maintenance_windows(tmp_path, monkeypatch):
    window = MaintenanceWindow(id="w", output_id="OUT_001", start=NOW - 30 * H, end=NOW - 10 * H, reason="r", created_at=NOW - 40 * H)
    everything = MaintenanceWindow(id="a", output_id=None, start=NOW - 5 * H, end=NOW - 4 * H, reason="r", created_at=NOW - 40 * H)
    repo, _, loop = _run(tmp_path, monkeypatch, [], prepare=lambda r: (r.add_maintenance(window), r.add_maintenance(everything)), ticks=3)
    try:
        ranges = loop._active_incident_ranges(NOW + 3 * STEP)
        assert (window.start, window.end) in ranges["OUT_001"] and (everything.start, everything.end) in ranges["OUT_001"]
        assert (everything.start, everything.end) in ranges["OUT_003"] and (window.start, window.end) not in ranges["OUT_003"]
    finally:
        repo.dispose()


# -- re-balayage des seuils à partir des verdicts (analysis.evaluation --from-db) --------------------------------


def _judged_run(tmp_path, monkeypatch, judge_blip=True):
    """Vraie boucle sur le Mock : une longue chute sur OUT_001 (jugée vraie), une brève sur OUT_002 (jugée fausse)."""
    repo, _, _ = _run(tmp_path, monkeypatch, [_drop("OUT_001", 6, 40), _drop("OUT_002", 6, 10)])
    for inc in repo.list_incidents_between(NOW, NOW + 10 * H):
        if inc.output_id == "OUT_001" or judge_blip:
            verdict = IncidentVerdict.TRUE_POSITIVE if inc.output_id == "OUT_001" else IncidentVerdict.FALSE_POSITIVE
            repo.set_feedback(inc.id, verdict, "alice", NOW + 9 * H)
    return repo


def test_replay_from_storage_reproduces_the_live_run_then_applies_other_thresholds(tmp_path, monkeypatch):
    from network_monitor.analysis.evaluation import replay_from_storage

    repo = _judged_run(tmp_path, monkeypatch)
    try:
        same = replay_from_storage(repo, NOW, NOW + 10 * H)  # mêmes réglages : le rejeu est exact
        assert (same["true_positives"], same["true_positives_still_detected"]) == (1, 1)
        assert (same["false_positives"], same["false_positives_still_opened"]) == (1, 1)
        stricter = replay_from_storage(repo, NOW, NOW + 10 * H, {"minimum_duration": "40m"})
        assert stricter["false_positives_still_opened"] == 0 and stricter["true_positives_still_detected"] == 1  # la coupure brève n'ouvre plus
        assert stricter["recall_on_judged"] == 1.0
        too_strict = replay_from_storage(repo, NOW, NOW + 10 * H, {"minimum_duration": "4h"})
        assert too_strict["true_positives_still_detected"] == 0 and too_strict["recall_on_judged"] == 0.0  # le vrai incident est perdu : visible
        looser = replay_from_storage(repo, NOW, NOW + 10 * H, {"minimum_duration": "5m"})
        assert looser["unjudged_ignored"] > 0  # de nouveaux incidents que personne n'a jugés : comptés à part, pas dans la précision
    finally:
        repo.dispose()


def test_replay_ignores_unjudged_incidents_and_reports_nothing_invented(tmp_path, monkeypatch):
    from network_monitor.analysis.evaluation import replay_from_storage

    repo = _judged_run(tmp_path, monkeypatch, judge_blip=False)
    try:
        result = replay_from_storage(repo, NOW, NOW + 10 * H)
        assert result["unjudged_original"] == 1  # l'incident de OUT_002 n'a pas de verdict : ignoré
        assert (result["false_positives"], result["false_positives_still_opened"]) == (0, 0)
        assert result["unjudged_ignored"] == 1  # et rejoué, il ne recoupe aucun incident jugé
        empty = replay_from_storage(repo, NOW + 20 * H, NOW + 30 * H)  # fenêtre sans aucun incident
        assert empty["true_positives"] == 0 and empty["recall_on_judged"] is None  # pas de pourcentage sans donnée
    finally:
        repo.dispose()


def test_evaluation_cli_sweeps_a_database_with_verdicts(tmp_path, monkeypatch):
    import ast
    import subprocess
    import sys

    repo = _judged_run(tmp_path, monkeypatch)
    repo.dispose()
    out = subprocess.run(
        [sys.executable, "-m", "network_monitor.analysis.evaluation", "--from-db", f"sqlite:///{tmp_path / 'loop.db'}",
         "--days", "1", "--end", (NOW + 10 * H).isoformat(), "--sweep"],
        capture_output=True, text=True, check=True, cwd=".",
    ).stdout.splitlines()
    assert out[0] == "2 incident(s) jugé(s), 0 sans verdict (ignorés)"
    rows = [ast.literal_eval(line) for line in out[1:]]
    assert rows and rows[0]["false_positives_still_opened"] == 0 and rows[0]["true_positives_still_detected"] == 1  # le meilleur réglage est trié en tête
    assert any(r["false_positives_still_opened"] == 1 for r in rows)  # et la grille contient aussi des réglages qui gardent le faux positif
