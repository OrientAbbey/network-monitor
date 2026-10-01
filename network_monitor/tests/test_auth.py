"""Authentification des endpoints de contrôle : mots de passe, jetons de session, limitation d'essais."""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.app import auth
from network_monitor.app.config import AppConfig

SECRET = b"k" * 32


def test_password_hash_roundtrip_and_salting():
    h1, h2 = auth.hash_password("correct horse"), auth.hash_password("correct horse")
    assert h1 != h2  # sel aléatoire : deux hachages du même mot de passe diffèrent
    assert auth.verify_password("correct horse", h1)
    assert not auth.verify_password("correct horsE", h1)
    assert not auth.verify_password("x", "not-a-hash") and not auth.verify_password("x", "md5$1$2$3$4$5")


def test_token_roundtrip_expiry_and_tampering():
    now = auth.utcnow()
    token = auth.issue_token("alice", SECRET, timedelta(minutes=10), now)
    assert auth.verify_token(token, SECRET, now + timedelta(minutes=9)) == "alice"
    assert auth.verify_token(token, SECRET, now + timedelta(minutes=10)) is None  # expiré
    assert auth.verify_token(token, b"z" * 32, now) is None  # autre clé
    payload, signature = token.split(".")
    forged = auth._b64(json.dumps({"sub": "root", "exp": 9_999_999_999}).encode())
    assert auth.verify_token(f"{forged}.{signature}", SECRET, now) is None  # charge utile modifiée
    for junk in ("", ".", "a.b", "pas un jeton", payload):
        assert auth.verify_token(junk, SECRET, now) is None


def test_throttle_blocks_then_expires():
    t, now = auth.LoginThrottle(), auth.utcnow()
    for _ in range(5):
        assert not t.blocked("alice", "1.2.3.4", now)
        t.record_failure("alice", "1.2.3.4", now)
    assert t.blocked("alice", "1.2.3.4", now)
    assert not t.blocked("alice", "5.6.7.8", now) and not t.blocked("bob", "1.2.3.4", now)
    assert not t.blocked("alice", "1.2.3.4", now + timedelta(minutes=6))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NM_USERS", json.dumps({"alice": auth.hash_password("s3cret-pass")}))
    monkeypatch.setenv("NM_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("NM_API_TOKEN", "machine-token")
    monkeypatch.setattr(api_module, "_throttle", auth.LoginThrottle())
    state = api_module.MonitoringState(config=AppConfig.model_validate({"history": {"minimum": "30d", "preferred": "40d"}}), db_url=f"sqlite:///{tmp_path / 't.db'}")
    api_module.state = state
    with TestClient(api_module.app) as c:
        state.stop()  # ces tests n'ont pas besoin de la boucle
        yield c
    state.storage.dispose()


def _login(client, password="s3cret-pass", user="alice"):
    return client.post("/auth/login", json={"username": user, "password": password})


def test_login_gives_a_token_that_unlocks_control_endpoints(client):
    r = _login(client)
    assert r.status_code == 200 and r.json()["username"] == "alice" and r.json()["expires_in"] == 3600
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.put("/config", json={"open_score": 0.7}, headers=headers).status_code == 200
    assert client.put("/config", json={"open_score": 0.7}).status_code == 401
    assert client.put("/config", json={"open_score": 0.7}, headers={"Authorization": "Bearer forged.token"}).status_code == 401


def test_machine_token_still_works(client):
    assert client.post("/monitoring/stop", headers={"Authorization": "Bearer machine-token"}).status_code == 200


def test_wrong_credentials_are_indistinguishable(client):
    unknown, wrong = _login(client, user="mallory"), _login(client, password="nope")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_login_is_rate_limited(client):
    assert [_login(client, password="x").status_code for _ in range(5)] == [401] * 5
    blocked = _login(client)  # même avec le bon mot de passe
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "300"


def test_expired_session_is_refused(client, monkeypatch):
    token = _login(client).json()["access_token"]
    later = auth.utcnow() + timedelta(hours=2)
    monkeypatch.setattr(auth, "utcnow", lambda: later)
    assert client.put("/config", json={"open_score": 0.7}, headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_fails_closed_without_any_credential_configured(client, monkeypatch):
    monkeypatch.delenv("NM_USERS")
    monkeypatch.delenv("NM_API_TOKEN")
    assert client.post("/monitoring/stop", headers={"Authorization": "Bearer anything"}).status_code == 503
