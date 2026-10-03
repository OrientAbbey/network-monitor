"""Alertes sortantes : file transactionnelle, regroupement, plafonds, nouvelles tentatives, canaux."""

import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.app.config import AppConfig, NotificationsConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import IncidentType, Severity
from network_monitor.app.notify import (
    NotificationDispatcher,
    alert_to_dict,
    format_sms,
    format_text,
)
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.incidents.manager import Alert
from network_monitor.storage.repository import SqlAlchemyRepository

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


def _alert(event="opened", severity=Severity.CRITICAL, minute=0, output="OUT_001", ident="inc-1", **kw):
    base = {
        "incident_id": ident, "output_id": output, "severity": severity, "event": event,
        "timestamp": T0 + timedelta(minutes=minute), "message": "m", "incident_type": IncidentType.TRAFFIC_DROP,
        "duration_seconds": 1800.0, "traffic_mbps": 100.0, "expected_traffic_mbps": 250.0,
        "traffic_deviation_percent": -60.0, "loss_percent": 0.4, "started_at": T0 - timedelta(minutes=10),
        "ended_at": None, "confirmed": True,
    }
    return Alert(**(base | kw))


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakeServers:
    """Transport httpx qui joue Telegram, la passerelle SMS Android et l'API Orange."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.telegram: list[httpx.Response] = []  # réponses programmées, sinon 200 ok
        self.gateway: list[httpx.Response] = []
        self.orange_fail: set[str] = set()
        self.orange_sent: list[str] = []
        self.token_requests = 0
        self.expire_token_once = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "api.telegram.org":
            return self.telegram.pop(0) if self.telegram else httpx.Response(200, json={"ok": True})
        if host == "gateway.local":
            return self.gateway.pop(0) if self.gateway else httpx.Response(202, json={"state": "Pending"})
        if request.url.path == "/oauth/v3/token":
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": f"tok{self.token_requests}", "expires_in": 3600})
        if "smsmessaging" in request.url.path:
            if self.expire_token_once:
                self.expire_token_once = False
                return httpx.Response(401, json={"message": "Expired"})
            number = json.loads(request.content)["outboundSMSMessageRequest"]["address"]
            if number in self.orange_fail:
                return httpx.Response(503, json={"message": "unavailable"})
            self.orange_sent.append(number)
            return httpx.Response(201, json={})
        return httpx.Response(404)


@pytest.fixture
def env(monkeypatch):
    for k, v in {
        "NM_TELEGRAM_TOKEN": "123:SECRET-TOKEN", "NM_SMS_URL": "http://gateway.local:8080", "NM_SMS_USER": "u",
        "NM_SMS_PASSWORD": "p", "NM_ORANGE_CLIENT_ID": "id", "NM_ORANGE_CLIENT_SECRET": "sec",
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def repo(tmp_path):
    r = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'n.db'}")
    yield r
    r.dispose()


def _dispatcher(repo, channels, servers=None, clock=None, **cfg):
    config = NotificationsConfig.model_validate({"enabled": True, "channels": channels, "timezone": "Africa/Douala", "batch_window": "20s", **cfg})
    servers = servers or FakeServers()
    clock = clock or Clock()
    client = httpx.Client(transport=httpx.MockTransport(servers.handler))
    return NotificationDispatcher(config, repo, client, clock), servers, clock


def _enqueue(repo, d, alerts):
    repo.save_tick([], [], [], [], alerts, d.plan(alerts))


TG = {"name": "tg", "type": "telegram", "chat_id": "-1001234"}
SMS = {"name": "sms", "type": "sms", "provider": "android_gateway", "recipients": ["+237690000001", "+237690000002"]}


# -- planification ------------------------------------------------------------------------------------


def test_plan_applies_severity_event_and_confirmation_filters(env, repo):
    d, *_ = _dispatcher(repo, [TG, SMS])
    plan = d.plan([
        _alert("opened", Severity.CRITICAL),      # tg + sms
        _alert("opened", Severity.WARNING, ident="b"),  # tg seulement (SMS : critique)
        _alert("suspect", Severity.WARNING, ident="c"),  # jamais
        _alert("updated", Severity.CRITICAL, ident="d"),  # tg (sms : opened/closed seulement)
        _alert("closed", Severity.INFO, ident="e", confirmed=False),  # bruit résorbé : jamais
    ])
    assert sorted((n.channel, n.alert["incident_id"]) for n in plan) == [("sms", "inc-1"), ("tg", "b"), ("tg", "d"), ("tg", "inc-1")]
    assert d.plan([]) == []
    assert len({n.id for n in plan}) == len(plan)


def test_notifications_disabled_plan_nothing(env, repo):
    config = NotificationsConfig.model_validate({"enabled": False, "channels": [TG]})
    assert NotificationDispatcher(config, repo, httpx.Client(transport=httpx.MockTransport(FakeServers().handler))).plan([_alert()]) == []


def test_missing_secret_refuses_to_start(monkeypatch, repo):
    monkeypatch.delenv("NM_TELEGRAM_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="NM_TELEGRAM_TOKEN"):
        _dispatcher(repo, [TG])


def test_config_rejects_bad_values_and_never_holds_secrets():
    with pytest.raises(ValueError, match="international"):
        NotificationsConfig.model_validate({"channels": [{**SMS, "recipients": ["0690000001"]}]})
    with pytest.raises(ValueError, match="inconnus"):
        NotificationsConfig.model_validate({"channels": [{**TG, "events": ["boom"]}]})
    with pytest.raises(ValueError, match="même nom"):
        NotificationsConfig.model_validate({"channels": [TG, TG]})
    with pytest.raises(ValueError, match="fuseau"):
        NotificationsConfig.model_validate({"timezone": "Mars/Olympus"})
    with pytest.raises(ValueError, match="sender"):
        NotificationsConfig.model_validate({"channels": [{**SMS, "provider": "orange"}]})
    cfg = AppConfig.model_validate({"notifications": {"enabled": True, "channels": [TG]}})
    assert "SECRET" not in cfg.model_dump_json() and "token_env" in cfg.model_dump_json()  # le nom de variable, pas sa valeur


# -- regroupement, fuseau et contenu --------------------------------------------------------------


def test_alerts_within_the_batch_window_leave_in_one_message(env, repo):
    d, servers, clock = _dispatcher(repo, [TG])
    _enqueue(repo, d, [_alert(ident="a", output="OUT_001"), _alert(ident="b", output="OUT_002"), _alert(ident="c", output="OUT_003")])
    assert d.run_once() == 0 and servers.requests == []  # fenêtre de 20 s pas écoulée : rien ne part
    clock.advance(seconds=21)
    assert d.run_once() == 1
    assert len(servers.requests) == 1  # trois alertes, un seul message (Telegram limite le débit par groupe)
    text = json.loads(servers.requests[0].content)["text"]
    assert text.startswith("3 alertes") and all(o in text for o in ("OUT_001", "OUT_002", "OUT_003"))
    assert {n.status for n in repo.list_notifications()} == {"sent"}


def test_message_uses_retro_dated_start_local_time_and_real_duration():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Africa/Douala")  # UTC+1
    opened = format_text([alert_to_dict(_alert("opened", minute=20, started_at=T0))], tz)
    assert "INCIDENT — OUT_001 : chute de trafic (critique)" in opened
    assert "Début : 13:00 (confirmé 13:20)" in opened  # 12:00 UTC = 13:00 à Douala ; la confirmation vient après
    assert "100 Mbps (attendu 250, -60 %)" in opened
    ended = T0 + timedelta(minutes=35)
    closed = format_text([alert_to_dict(_alert("closed", minute=50, started_at=T0, ended_at=ended, duration_seconds=2100.0))], tz)
    assert "RÉSOLU" in closed and "Durée : 35 min (13:00 → 13:35)" in closed


def test_sms_text_is_a_single_ascii_segment():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Africa/Douala")
    cases = [
        [_alert()], [_alert("closed", duration_seconds=7500.0)],
        [_alert(ident=str(i), output=f"OUT_{i:04d}") for i in range(30)],
        [_alert(incident_type=IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS)],
    ]
    for alerts in cases:
        sms = format_sms([alert_to_dict(a) for a in alerts], tz)
        assert len(sms) <= 160 and sms.isascii(), sms
    assert "chute de trafic avec pertes" in format_sms([alert_to_dict(cases[3][0])], tz)  # accents retirés, sens conservé
    assert format_sms([alert_to_dict(_alert("closed", duration_seconds=7500.0))], tz).startswith("RESOLU OUT_001 chute de trafic, duree 2 h 05")


# -- Telegram -----------------------------------------------------------------------------------------


def test_telegram_posts_to_the_group_and_hides_the_token_in_errors(env, repo):
    d, servers, clock = _dispatcher(repo, [TG])
    servers.telegram = [httpx.Response(400, json={"ok": False, "description": "chat not found 123:SECRET-TOKEN"})]
    _enqueue(repo, d, [_alert()])
    clock.advance(seconds=21)
    d.run_once()
    req = servers.requests[0]
    assert req.url.path == "/bot123:SECRET-TOKEN/sendMessage" and json.loads(req.content)["chat_id"] == "-1001234"
    [n] = repo.list_notifications()
    assert n.status == "failed" and n.attempts == 1  # 400 : définitif, jamais retenté
    assert "SECRET-TOKEN" not in (n.last_error or "") and "***" in (n.last_error or "")


def test_telegram_429_is_retried_after_the_delay_telegram_asks_for(env, repo):
    d, servers, clock = _dispatcher(repo, [TG])
    servers.telegram = [httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 120}})]
    _enqueue(repo, d, [_alert()])
    clock.advance(seconds=21)
    d.run_once()
    [n] = repo.list_notifications()
    assert n.status == "pending" and n.attempts == 1
    assert n.next_attempt_at >= clock.now + timedelta(seconds=120)  # au moins ce que Telegram exige
    clock.advance(seconds=100)
    assert d.run_once() == 0  # trop tôt
    clock.advance(seconds=30)
    assert d.run_once() == 1 and repo.list_notifications()[0].status == "sent"


def test_retries_back_off_then_give_up(env, repo):
    d, servers, clock = _dispatcher(repo, [TG], max_attempts=3, retry_base="30s")
    servers.telegram = [httpx.Response(503)] * 5
    _enqueue(repo, d, [_alert()])
    gaps = []
    for _ in range(4):
        clock.advance(minutes=20)
        d.run_once()
        [n] = repo.list_notifications()
        gaps.append((n.status, n.attempts))
    assert gaps[0] == ("pending", 1) and gaps[1] == ("pending", 2) and gaps[2] == ("failed", 3) and gaps[3] == ("failed", 3)
    assert len(servers.requests) == 3  # plus aucune tentative une fois abandonné


def test_a_network_error_is_retryable(env, repo):
    config = NotificationsConfig.model_validate({"enabled": True, "channels": [TG]})
    clock = Clock()

    def boom(request):
        raise httpx.ConnectError("réseau coupé")

    d = NotificationDispatcher(config, repo, httpx.Client(transport=httpx.MockTransport(boom)), clock)
    _enqueue(repo, d, [_alert()])
    clock.advance(seconds=21)
    d.run_once()
    [n] = repo.list_notifications()
    assert n.status == "pending" and "ConnectError" in (n.last_error or "")


def test_outbox_survives_a_restart(env, repo):
    d1, _, clock = _dispatcher(repo, [TG])
    _enqueue(repo, d1, [_alert()])
    d2, servers2, _ = _dispatcher(repo, [TG], clock=clock)  # « redémarrage » : nouvelle instance, même base
    clock.advance(seconds=21)
    assert d2.run_once() == 1 and len(servers2.requests) == 1


# -- plafond de messages -----------------------------------------------------------------------------


def test_rate_limit_defers_and_coalesces_instead_of_dropping(env, repo):
    d, servers, clock = _dispatcher(repo, [{**TG, "max_messages_per_hour": 2, "max_messages_per_minute": None}])
    for i in range(2):  # deux messages séparés dans la limite
        _enqueue(repo, d, [_alert(ident=f"x{i}", output=f"OUT_{i}")])
        clock.advance(seconds=21)
        assert d.run_once() == 1
    for i in range(3):  # trois alertes de plus pendant que le plafond est atteint
        _enqueue(repo, d, [_alert(ident=f"y{i}", output=f"OUT_{i + 10}")])
        clock.advance(seconds=21)
        assert d.run_once() == 0
    assert len(servers.requests) == 2
    assert {n.status for n in repo.list_notifications(status="pending")} == {"pending"}
    clock.advance(hours=1)
    assert d.run_once() == 1  # la fenêtre se libère : les trois partent ensemble
    assert json.loads(servers.requests[-1].content)["text"].startswith("3 alertes")
    assert all(n.status == "sent" for n in repo.list_notifications())


# -- SMS de groupe ---------------------------------------------------------------------------------


def test_android_gateway_sends_one_call_for_the_whole_group(env, repo):
    d, servers, clock = _dispatcher(repo, [SMS])
    _enqueue(repo, d, [_alert()])
    clock.advance(seconds=21)
    d.run_once()
    [req] = servers.requests
    body = json.loads(req.content)
    assert req.url.path == "/messages" and body["phoneNumbers"] == ["+237690000001", "+237690000002"]
    assert body["textMessage"]["text"].startswith("CRIT OUT_001 chute de trafic depuis 12:50") and len(body["textMessage"]["text"]) <= 160
    assert req.headers["authorization"].startswith("Basic ")


def test_orange_retries_only_the_recipients_that_failed(env, repo):
    chan = {"name": "sms", "type": "sms", "provider": "orange", "sender": "+237690000000", "recipients": ["+237690000001", "+237690000002", "+237690000003"]}
    d, servers, clock = _dispatcher(repo, [chan])
    servers.orange_fail = {"tel:+237690000002"}
    _enqueue(repo, d, [_alert()])
    clock.advance(seconds=21)
    d.run_once()
    assert servers.orange_sent == ["tel:+237690000001", "tel:+237690000003"] and servers.token_requests == 1
    [n] = repo.list_notifications()
    assert n.status == "pending" and n.pending_targets == ["+237690000002"]
    servers.orange_fail = set()
    clock.advance(minutes=5)
    d.run_once()
    assert servers.orange_sent.count("tel:+237690000001") == 1  # jamais deux SMS au même numéro
    assert servers.orange_sent[-1] == "tel:+237690000002" and repo.list_notifications()[0].status == "sent"
    assert servers.token_requests == 1  # jeton réutilisé


def test_orange_refreshes_an_expired_token(env, repo):
    chan = {"name": "sms", "type": "sms", "provider": "orange", "sender": "+237690000000", "recipients": ["+237690000001"]}
    d, servers, clock = _dispatcher(repo, [chan])
    servers.expire_token_once = True
    _enqueue(repo, d, [_alert()])
    clock.advance(seconds=21)
    d.run_once()
    assert repo.list_notifications()[0].status == "pending"  # refusé une fois
    clock.advance(minutes=5)
    d.run_once()
    assert servers.token_requests == 2 and repo.list_notifications()[0].status == "sent"  # nouveau jeton demandé


# -- webhook (vrai serveur HTTP local) --------------------------------------------------------------


class _Receiver(BaseHTTPRequestHandler):
    log: ClassVar[list] = []
    fail_first = 0

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).log.append((dict(self.headers), body))
        if type(self).fail_first > 0:
            type(self).fail_first -= 1
            self.send_response(500)
        else:
            self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver():
    _Receiver.log, _Receiver.fail_first = [], 0
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def test_webhook_signature_and_stable_delivery_id_across_retries(env, repo, receiver, monkeypatch):
    monkeypatch.setenv("NM_WEBHOOK_URL", f"http://127.0.0.1:{receiver.server_port}/hook")
    monkeypatch.setenv("NM_WEBHOOK_SECRET", "topsecret")
    _Receiver.fail_first = 1
    config = NotificationsConfig.model_validate({"enabled": True, "channels": [{"name": "wh", "type": "webhook"}], "retry_base": "30s"})
    clock = Clock()
    d = NotificationDispatcher(config, repo, httpx.Client(timeout=5), clock)  # vrai réseau, vrai serveur
    _enqueue(repo, d, [_alert()])
    clock.advance(seconds=21)
    d.run_once()  # 500
    clock.advance(minutes=5)
    d.run_once()  # 204
    assert [n.status for n in repo.list_notifications()] == ["sent"] and len(_Receiver.log) == 2
    (h1, b1), (h2, b2) = _Receiver.log
    assert h1["X-NM-Delivery"] == h2["X-NM-Delivery"]  # le récepteur peut dédupliquer
    for headers, body in ((h1, b1), (h2, b2)):
        expected = hmac.new(b"topsecret", headers["X-NM-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(headers["X-NM-Signature"], f"sha256={expected}")  # authenticité vérifiable
    payload = json.loads(b1)
    assert payload["alerts"][0]["output_id"] == "OUT_001" and "INCIDENT" in payload["text"]


def test_webhook_url_must_be_http(env, repo, monkeypatch):
    monkeypatch.setenv("NM_WEBHOOK_URL", "file:///etc/passwd")
    with pytest.raises(RuntimeError, match="http"):
        _dispatcher(repo, [{"name": "wh", "type": "webhook"}])


# -- de bout en bout : un vrai scénario d'incident ----------------------------------------------------


def test_a_real_incident_notifies_when_opened_and_when_resolved_but_not_for_noise(env, tmp_path):
    NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
    cfg = AppConfig.model_validate({
        "history": {"minimum": "7d", "preferred": "10d"},
        "notifications": {"enabled": True, "channels": [TG], "batch_window": "0s", "timezone": "Africa/Douala"},
    })
    source = MockDataSource(seed=1)
    source.add_scenario(AnomalyScenario(
        scenario_id="S", output_id="OUT_001", start_time=NOW + 6 * STEP, end_time=NOW + 40 * STEP,
        type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL, traffic_drop_fraction=0.7,
    ))
    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'e2e.db'}")
    servers = FakeServers()
    clock = Clock()
    try:
        dispatcher = NotificationDispatcher(cfg.notifications, repo, httpx.Client(transport=httpx.MockTransport(servers.handler)), clock)
        loop = MonitoringLoop(source, repo, cfg, dispatcher)
        loop.calibrate(NOW)
        source.start_realtime(NOW)
        for k in range(1, 60):
            loop.run_tick(NOW + k * STEP)
            clock.now = NOW + k * STEP
            dispatcher.run_once()
        texts = [json.loads(r.content)["text"] for r in servers.requests]
        assert any(t.startswith("🔴 INCIDENT — OUT_001") for t in texts)
        resolved = [t for t in texts if t.startswith("🟢 RÉSOLU — OUT_001")]
        assert len(resolved) == 1 and "Durée : " in resolved[0]
        assert all("OUT_002" not in t and "OUT_003" not in t for t in texts)  # le bruit des autres sorties n'a rien déclenché
        assert not any("suspect" in t.lower() for t in texts)
    finally:
        repo.dispose()


# -- API ----------------------------------------------------------------------------------------------


def test_api_notification_endpoints_require_authentication_and_test_a_channel(env, tmp_path, monkeypatch):
    monkeypatch.setenv("NM_API_TOKEN", "tok")
    cfg = AppConfig.model_validate({"history": {"minimum": "30d", "preferred": "40d"}, "notifications": {"enabled": True, "channels": [TG]}})
    state = api_module.MonitoringState(config=cfg, db_url=f"sqlite:///{tmp_path / 'a.db'}")
    servers = FakeServers()
    assert state.notifier is not None
    state.notifier._client = httpx.Client(transport=httpx.MockTransport(servers.handler))
    for channel in state.notifier.channels.values():
        channel._client = state.notifier._client
    api_module.state = state
    auth = {"Authorization": "Bearer tok"}
    with TestClient(api_module.app) as c:
        state.stop()
        assert c.get("/notifications").status_code == 401
        assert c.post("/notifications/test/tg").status_code == 401
        assert c.post("/notifications/test/nope", headers=auth).status_code == 404
        r = c.post("/notifications/test/tg", headers=auth)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert json.loads(servers.requests[-1].content)["chat_id"] == "-1001234"
        assert c.get("/notifications", headers=auth).json() == []
    state.storage.dispose()
