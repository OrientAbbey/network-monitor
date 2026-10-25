"""Supervision : `/metrics` (format Prometheus), `/health/ready`, télémétrie de la boucle, signal de vie externe."""

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.app.config import AppConfig, HeartbeatConfig
from network_monitor.app.metrics import render
from network_monitor.app.notify import Heartbeat
from network_monitor.storage.repository import Notification

SECRET = "TOPSECRET-uuid-1234"


# -- format Prometheus --------------------------------------------------------------------------------------


def test_render_escapes_labels_and_help_and_formats_numbers():
    text = render([
        ("nm_x", "aide avec \\ et\nsaut", "gauge", [({"output": 'a"b\\c\nd'}, 1.5), ({}, 3), ({"k": "v"}, float("nan")), ({"k": "w"}, float("inf"))]),
        ("nm_vide", "aucune valeur : famille omise", "gauge", []),
        ("nm_n_total", "compteur", "counter", [({}, True)]),
    ])
    lines = text.splitlines()
    assert lines[0] == "# HELP nm_x aide avec \\\\ et\\nsaut"  # antislash et saut de ligne échappés dans l'aide
    assert lines[1] == "# TYPE nm_x gauge"
    assert 'nm_x{output="a\\"b\\\\c\\nd"} 1.5' in lines  # guillemet, antislash et saut de ligne échappés dans l'étiquette
    assert "nm_x 3" in lines and 'nm_x{k="v"} NaN' in lines and 'nm_x{k="w"} +Inf' in lines
    assert "nm_n_total 1" in lines and "nm_vide" not in text and text.endswith("\n")
    for line in lines:  # chaque ligne d'échantillon respecte la grammaire de l'exposition texte
        assert line.startswith("#") or re.fullmatch(r'[a-z_]+(\{([a-z]+="([^"\\]|\\.)*",?)+\})? (-?[\d.]+|NaN|\+Inf|-Inf)', line), line


def parse(text: str) -> dict[str, float]:
    """Lit l'exposition texte : « nom{étiquettes} valeur » → valeur."""
    return {m[1]: float(m[2]) for line in text.splitlines() if (m := re.fullmatch(r"((?:\S+?(?:\{.*\})?)) (\S+)", line)) and not line.startswith("#")}


# -- service complet ---------------------------------------------------------------------------------------


def _make_state(tmp_path, monkeypatch, **extra):
    cfg = {"history": {"minimum": "7d", "preferred": "10d"}, **extra}
    state = api_module.MonitoringState(config=AppConfig.model_validate(cfg), db_url=f"sqlite:///{tmp_path / 'o.db'}")
    state.seconds_per_tick = 0.05
    monkeypatch.setattr(api_module, "state", state)
    return state


def _wait(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def client(tmp_path, monkeypatch):
    state = _make_state(tmp_path, monkeypatch)
    with TestClient(api_module.app) as c:
        assert _wait(lambda: state.telemetry.ticks >= 3 and len(state.loop.last_sample_at) == 3)
        yield c
    state.storage.dispose()


def _freeze(state, **telemetry):
    """Arrête la boucle (plus de pas qui écrasent les valeurs) et la déclare « en marche » avec une télémétrie choisie."""
    state.stop()
    state.running = True
    state.telemetry = api_module.Telemetry(**{"ticks": 5, "last_tick_at": time.time(), **telemetry})
    interval = state.source.get_interval()
    state.loop.last_sample_at = {o: state.now - interval for o in state.source.list_outputs()}


def test_metrics_exposes_the_loop_state_in_prometheus_text(client):
    res = client.get("/metrics")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/plain; version=0.0.4")
    m = parse(res.text)
    state = api_module.state
    assert m["nm_up"] == 1 and m["nm_monitoring_running"] == 1 and m["nm_database_ok"] == 1
    assert m["nm_ticks_total"] >= 3 and m["nm_tick_errors_total"] == 0
    assert m["nm_last_tick_duration_seconds"] >= 0 and abs(m["nm_last_tick_timestamp_seconds"] - time.time()) < 10
    assert m["nm_interval_seconds"] == 300
    assert m["nm_simulated_clock_timestamp_seconds"] == pytest.approx(state.now.timestamp(), abs=60)
    assert m['nm_build_info{version="1.0.0"}'] == 1
    for o in ("OUT_001", "OUT_002", "OUT_003"):
        assert m[f'nm_output_calibrated{{output="{o}"}}'] == 1
        assert m[f'nm_output_last_sample_timestamp_seconds{{output="{o}"}}'] == state.loop.last_sample_at[o].timestamp()
        for status in ("suspect", "open", "stale"):  # des zéros explicites : une règle d'alerte ne doit pas dépendre d'une série absente
            assert m[f'nm_active_incidents{{output="{o}",status="{status}"}}'] == 0


def test_metrics_token_is_optional_and_compared_safely(client, monkeypatch):
    assert client.get("/metrics").status_code == 200  # sans jeton configuré : ouvert, comme les autres lectures
    monkeypatch.setenv("NM_METRICS_TOKEN", "s3cret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer faux"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "s3cret"}).status_code == 401  # le schéma Bearer est exigé
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and "nm_up 1" in ok.text


def test_metrics_counts_notifications_per_channel_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("NM_TELEGRAM_TOKEN", "t")
    channel = {"name": "tg", "type": "telegram", "chat_id": "-1"}
    state = _make_state(tmp_path, monkeypatch, notifications={"enabled": True, "channels": [channel]})
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with TestClient(api_module.app) as c:
        state.stop()
        state.storage.update_notifications([
            Notification(id=f"n{i}", channel="tg", alert={}, created_at=now, next_attempt_at=now, status=status)
            for i, status in enumerate(["failed", "failed", "sent"])
        ])
        m = parse(c.get("/metrics").text)
    state.storage.dispose()
    assert (m['nm_notifications{channel="tg",status="failed"}'], m['nm_notifications{channel="tg",status="sent"}']) == (2, 1)
    assert m['nm_notifications{channel="tg",status="pending"}'] == 0  # série présente à zéro


def test_ready_is_ok_when_the_loop_runs_and_data_is_fresh(client):
    res = client.get("/health/ready")
    assert res.status_code == 200 and res.json() == {"status": "ok", "reasons": []}
    assert client.get("/health").json() == {"status": "ok"}  # la vivacité simple reste distincte


def test_ready_explains_each_failure_with_503(client, monkeypatch):
    state = api_module.state
    interval = state.source.get_interval()

    def reasons():
        res = client.get("/health/ready")
        assert (res.status_code == 503) == bool(res.json()["reasons"])
        return " | ".join(res.json()["reasons"])

    _freeze(state)
    assert reasons() == ""

    state.loop.last_sample_at["OUT_002"] = state.now - 4 * interval  # 3 intervalles = limite : au-delà, périmé
    stale = reasons()
    assert "données périmées pour OUT_002" in stale and "OUT_001" not in stale and "OUT_003" not in stale
    state.loop.last_sample_at["OUT_002"] = state.now - 2 * interval
    assert reasons() == ""  # encore dans la tolérance

    del state.loop.last_sample_at["OUT_003"]
    assert "aucune mesure reçue pour OUT_003" in reasons()
    _freeze(state, ticks=0)  # avant le premier pas, aucune mesure n'est encore attendue
    state.loop.last_sample_at.clear()
    assert reasons() == ""

    _freeze(state, last_tick_at=time.time() - (3 * state.seconds_per_tick + 11))  # marge de 10 s dépassée
    assert "aucun pas de surveillance depuis" in reasons()
    _freeze(state, last_tick_at=time.time() - (3 * state.seconds_per_tick + 5))  # dans la marge : pas une panne
    assert reasons() == ""

    monkeypatch.setattr(state.storage, "ping", lambda: False)
    assert "base de données inaccessible" in reasons()
    metrics = parse(client.get("/metrics").text)  # une base HS ne casse pas /metrics : elle s'y voit
    assert client.get("/metrics").status_code == 200 and metrics["nm_database_ok"] == 0
    assert not any(k.startswith("nm_active_incidents") for k in metrics)
    monkeypatch.undo()

    state.running = False  # arrêt voulu par l'opérateur
    assert reasons() == "boucle de surveillance arrêtée"
    assert parse(client.get("/metrics").text)["nm_monitoring_running"] == 0


def test_a_failing_tick_is_counted_and_does_not_kill_the_loop(client, monkeypatch):
    state = api_module.state
    state.stop()

    def boom(_now):
        raise RuntimeError("source illisible")

    monkeypatch.setattr(state.loop, "run_tick", boom)
    before = state.telemetry.errors
    state.start()
    assert _wait(lambda: state.telemetry.errors >= before + 3)  # la boucle continue malgré les erreurs
    state.stop()
    m = parse(client.get("/metrics").text)
    assert m["nm_tick_errors_total"] >= before + 3 and m["nm_ticks_total"] >= m["nm_tick_errors_total"]


# -- signal de vie ------------------------------------------------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        self.server.hits.append((self.path, body))  # type: ignore[attr-defined]
        self.send_response(self.server.status)  # type: ignore[attr-defined]
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def ping_server():
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    server.hits, server.status = [], 200  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _cfg(**kw):
    return HeartbeatConfig(enabled=True, period=timedelta(milliseconds=50), timeout=timedelta(seconds=2), **kw)


def _beater(monkeypatch, server, check, path=f"/ping/{SECRET}"):
    monkeypatch.setenv("NM_HEARTBEAT_URL", f"http://127.0.0.1:{server.server_port}{path}")
    return Heartbeat(_cfg(), check)


def test_heartbeat_pings_when_healthy_and_calls_fail_with_the_reasons_otherwise(ping_server, monkeypatch):
    reasons: list[str] = []
    hb = _beater(monkeypatch, ping_server, lambda: reasons)
    try:
        assert hb.beat() is True
        reasons += ["base de données inaccessible", "boucle de surveillance arrêtée"]
        assert hb.beat() is True
    finally:
        hb._client.close()
    assert ping_server.hits == [
        (f"/ping/{SECRET}", ""),
        (f"/ping/{SECRET}/fail", "base de données inaccessible; boucle de surveillance arrêtée"),
    ]


def test_heartbeat_tolerates_a_trailing_slash_and_a_crashing_check(ping_server, monkeypatch, caplog):
    def crash():
        raise ValueError("boum")

    hb = _beater(monkeypatch, ping_server, crash, path=f"/ping/{SECRET}/")
    with caplog.at_level(logging.WARNING):
        assert hb.beat() is True
    hb._client.close()
    assert ping_server.hits == [(f"/ping/{SECRET}/fail", "contrôle de santé en erreur (ValueError)")]  # pas de « // » avant fail


def test_heartbeat_never_raises_nor_leaks_the_url_when_the_service_is_down(ping_server, monkeypatch, caplog):
    ping_server.status = 500
    hb = _beater(monkeypatch, ping_server, list)
    with caplog.at_level(logging.DEBUG):
        assert hb.beat() is False  # le service tiers répond 500
        ping_server.shutdown()
        ping_server.server_close()
        assert hb.beat() is False  # puis il est injoignable
    hb._client.close()
    logged = caplog.text
    # Le contrat est le signalement d'indisponibilité, pas la classe réseau
    # choisie par l'OS (Windows peut expirer là où Linux renvoie ECONNREFUSED).
    assert "500" in logged and "service injoignable" in logged
    assert SECRET not in logged  # l'URL est un secret : jamais dans les journaux, même quand httpx est bavard


def test_heartbeat_requires_its_secret_in_the_environment(monkeypatch):
    monkeypatch.delenv("NM_HEARTBEAT_URL", raising=False)
    with pytest.raises(RuntimeError, match="NM_HEARTBEAT_URL"):
        Heartbeat(_cfg(), list)


def test_heartbeat_thread_beats_immediately_then_periodically_and_stops(ping_server, monkeypatch):
    hb = _beater(monkeypatch, ping_server, list)
    hb.start()
    assert _wait(lambda: len(ping_server.hits) >= 4)
    hb.stop()
    count = len(ping_server.hits)
    time.sleep(0.3)
    assert len(ping_server.hits) == count  # plus aucun appel après l'arrêt : c'est ce qui déclenche l'alerte chez le service


def test_the_service_pings_while_healthy_and_a_planned_shutdown_is_not_a_failure(tmp_path, monkeypatch, ping_server):
    monkeypatch.setenv("NM_HEARTBEAT_URL", f"http://127.0.0.1:{ping_server.server_port}/ping/{SECRET}")
    state = _make_state(tmp_path, monkeypatch, heartbeat={"enabled": True, "period": "1s"})
    with TestClient(api_module.app) as c:
        assert _wait(lambda: len(ping_server.hits) >= 2, timeout=10)
        config = c.get("/config").text
        assert SECRET not in config and "NM_HEARTBEAT_URL" in config  # GET /config nomme la variable, jamais l'URL
    state.storage.dispose()
    assert {path for path, _ in ping_server.hits} == {f"/ping/{SECRET}"}  # jamais « /fail », y compris à l'arrêt


def test_heartbeat_config_parses_durations():
    cfg = AppConfig.model_validate({"heartbeat": {"enabled": True, "period": "30s", "timeout": "5s"}}).heartbeat
    assert (cfg.period, cfg.timeout, cfg.fail_suffix, cfg.url_env) == (timedelta(seconds=30), timedelta(seconds=5), "/fail", "NM_HEARTBEAT_URL")
    assert AppConfig().heartbeat.enabled is False
