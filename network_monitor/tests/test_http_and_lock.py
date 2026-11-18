"""Trafic HTTP (gzip, ETag), instance unique et service de l'interface."""

import json
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.app.config import AppConfig
from network_monitor.app.lock import AlreadyRunningError, SingleInstanceLock
from network_monitor.app.models import TrafficSample

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path):
    state = api_module.MonitoringState(
        config=AppConfig.model_validate({"history": {"minimum": "30d", "preferred": "40d"}}),
        db_url=f"sqlite:///{tmp_path / 'h.db'}",
    )
    state.now = T0 + timedelta(days=1)
    api_module.state = state
    state.storage.save_samples([
        TrafficSample(timestamp=T0 + i * timedelta(minutes=5), output_id="OUT_001", traffic_mbps=100.0 + i, loss_percent=0.1)
        for i in range(288)
    ])
    with TestClient(api_module.app) as c:
        state.stop()
        yield c
    state.storage.dispose()


def test_large_responses_are_gzipped_and_small_ones_are_not(client):
    path = "/outputs/OUT_001/history?span_hours=24"
    plain = client.get(path, headers={"Accept-Encoding": "identity"})
    zipped = client.get(path, headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in plain.headers and zipped.headers["content-encoding"] == "gzip"
    assert zipped.json() == plain.json() and len(plain.json()) == 288
    assert int(zipped.headers["content-length"]) < int(plain.headers["content-length"]) / 4  # JSON répétitif : ≥ 4× plus petit
    assert "content-encoding" not in client.get("/health", headers={"Accept-Encoding": "gzip"}).headers  # < 500 octets


def test_etag_gives_a_bodyless_304_until_the_data_changes(client):
    path = "/outputs/OUT_001/history?span_hours=24"
    first = client.get(path)
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "no-cache" and etag.startswith('W/"')
    again = client.get(path, headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    client.get("/health")  # une autre ressource ne casse rien
    api_module.state.storage.save_samples([TrafficSample(timestamp=T0 + timedelta(hours=23, minutes=55), output_id="OUT_001", traffic_mbps=999.0, loss_percent=0.1)])
    changed = client.get(path, headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag  # donnée modifiée : corps renvoyé
    assert client.get(path, headers={"If-None-Match": 'W/"autre", ' + changed.headers["etag"]}).status_code == 304


def test_304_and_gzip_work_together_and_keep_cors_headers(client):
    path = "/outputs/OUT_001/history?span_hours=24"
    headers = {"Accept-Encoding": "gzip", "Origin": "http://localhost:5173"}
    etag = client.get(path, headers=headers).headers["etag"]
    r = client.get(path, headers=headers | {"If-None-Match": etag})
    assert r.status_code == 304 and r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_only_get_is_conditional_and_errors_are_never_cached(client):
    assert client.post("/auth/login", json={"username": "x", "password": "y"}).status_code in (401, 503)
    missing = client.get("/outputs/NOPE/history")
    assert missing.status_code == 404 and "etag" not in missing.headers


def test_incremental_fetch_returns_only_new_points(client):
    last = (T0 + timedelta(hours=23, minutes=55)).isoformat()
    r = client.get("/outputs/OUT_001/history", params={"since": last})
    assert [p["timestamp"] for p in r.json()] == ["2026-09-01T23:55:00Z"]  # un seul point : le dernier connu
    assert len(r.content) < 200  # contre ~38 Ko pour la fenêtre entière


# -- instance unique -------------------------------------------------------------------------------


def test_lock_refuses_a_second_holder_and_frees_on_release(tmp_path):
    path = tmp_path / "x.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    second = SingleInstanceLock(path)
    with pytest.raises(AlreadyRunningError, match="--workers 1"):
        second.acquire()
    first.release()
    second.acquire()  # libéré : le suivant passe
    second.release()
    second.release()  # idempotent


def test_lock_is_held_across_processes_and_released_when_the_holder_dies(tmp_path):
    path = tmp_path / "p.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            from network_monitor.app.lock import SingleInstanceLock
            lock = SingleInstanceLock({str(path)!r})  # gardé vivant : sa destruction relâcherait le verrou
            lock.acquire()
            print("held", flush=True)
            time.sleep(60)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(AlreadyRunningError):
            SingleInstanceLock(path).acquire()
        holder.kill()  # mort brutale : le noyau relâche le verrou, aucun fichier orphelin ne bloque
        holder.wait(timeout=10)
        ours = SingleInstanceLock(path)
        ours.acquire()
        ours.release()
    finally:
        holder.kill()


def test_multiple_workers_are_refused_up_front(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(AlreadyRunningError, match="un seul worker"):
        SingleInstanceLock(tmp_path / "w.lock").acquire()


def test_a_second_app_instance_on_the_same_database_fails_to_start(tmp_path):
    cfg = AppConfig.model_validate({"history": {"minimum": "30d", "preferred": "40d"}})
    url = f"sqlite:///{tmp_path / 'same.db'}"
    a, b = api_module.MonitoringState(cfg, url), api_module.MonitoringState(cfg, url)
    a.lock.acquire()
    try:
        api_module.state = b
        with pytest.raises(AlreadyRunningError), TestClient(api_module.app):
            pass
    finally:
        a.lock.release()
        a.storage.dispose()
        b.storage.dispose()


# -- interface servie par l'API --------------------------------------------------------------------------


def test_static_frontend_is_served_after_the_api_routes(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<!doctype html><title>Signal Room</title>", encoding="utf-8")
    code = textwrap.dedent("""
        import json
        from fastapi.testclient import TestClient
        import network_monitor.app.api as api
        with TestClient(api.app) as c:
            api.state.stop()
            print(json.dumps([c.get("/").text, c.get("/health").json(), c.get("/outputs/NOPE/history").status_code]))
    """)
    import os

    env = os.environ | {"NM_STATIC_DIR": str(tmp_path / "ui"), "NM_ENV": "production",
                        "NM_CONFIG": str(_write_cfg(tmp_path)), "PYTHONPATH": os.getcwd()}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120, cwd=tmp_path, check=False)
    assert out.returncode == 0, out.stderr[-800:]
    page, health, missing = json.loads(out.stdout.strip().splitlines()[-1])
    assert "Signal Room" in page and health == {"status": "ok"} and missing == 404  # l'API gagne sur l'interface


def _write_cfg(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(f"history: {{minimum: 30d, preferred: 40d}}\nstorage: {{database_url: 'sqlite:///{tmp_path / 's.db'}'}}\n", encoding="utf-8")
    return p
