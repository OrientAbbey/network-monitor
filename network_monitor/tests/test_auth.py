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


# -- rôles ------------------------------------------------------------------------------------------------------------

PW = "s3cret-pass"


def _users(**extra):
    """alice : ancien format (donc admin) ; bob : opérateur ; carol : lecteur ; dave : administrateur explicite."""
    h = auth.hash_password(PW)
    return json.dumps({"alice": h, "bob": {"hash": h, "role": "operator"}, "carol": {"hash": h, "role": "viewer"}, "dave": {"hash": h, "role": "admin"}, **extra})


@pytest.fixture
def roles_client(client, monkeypatch):
    monkeypatch.setenv("NM_USERS", _users())
    return client


def _bearer(client, user):
    return {"Authorization": f"Bearer {_login(client, user=user).json()['access_token']}"}


# Une requête par endpoint protégé, choisie pour échouer APRÈS l'authentification si elle est autorisée (404 ou 200) :
# « autorisé » = ni 401 ni 403. Le 403 prime sur la validation du corps : ces requêtes n'ont donc pas besoin d'être valides.
PROTECTED = {
    "viewer": [("GET", "/notifications", None)],
    "operator": [
        ("PUT", "/incidents/nope/ack", None), ("DELETE", "/incidents/nope/ack", None),
        ("PUT", "/incidents/nope/feedback", {"verdict": "false_positive"}),
        ("POST", "/maintenance", {}), ("DELETE", "/maintenance/nope", None),
    ],
    "admin": [("PUT", "/config", {"open_score": 0.7}), ("POST", "/monitoring/stop", None), ("POST", "/monitoring/start", None),
              ("POST", "/notifications/test/nope", None)],
}
RANK = {"viewer": 1, "operator": 2, "admin": 3}


@pytest.mark.parametrize("user,role", [("carol", "viewer"), ("bob", "operator"), ("dave", "admin"), ("alice", "admin")])
def test_each_role_can_do_exactly_what_it_should(roles_client, user, role):
    headers = _bearer(roles_client, user)
    for needed, calls in PROTECTED.items():
        for method, path, body in calls:
            status = roles_client.request(method, path, json=body, headers=headers).status_code
            allowed = RANK[role] >= RANK[needed]
            assert (status not in (401, 403)) == allowed, f"{user} ({role}) {method} {path} -> {status}"
            if not allowed:
                assert status == 403


def test_refusal_names_the_required_and_the_actual_role(roles_client):
    r = roles_client.put("/config", json={"open_score": 0.7}, headers=_bearer(roles_client, "bob"))
    assert r.status_code == 403 and "« admin »" in r.json()["detail"] and "« operator »" in r.json()["detail"]
    assert roles_client.put("/config", json={"open_score": 0.7}).status_code == 401  # pas connecté : 401, pas 403


def test_login_returns_the_role_and_old_format_users_stay_admin(roles_client):
    roles = {u: _login(roles_client, user=u).json()["role"] for u in ("alice", "bob", "carol", "dave")}
    assert roles == {"alice": "admin", "bob": "operator", "carol": "viewer", "dave": "admin"}


def test_startup_warns_about_users_without_a_role(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("NM_USERS", _users())
    monkeypatch.setenv("NM_AUTH_SECRET", "test-secret")
    api_module.state = api_module.MonitoringState(
        config=AppConfig.model_validate({"history": {"minimum": "7d", "preferred": "10d"}}), db_url=f"sqlite:///{tmp_path / 'w.db'}"
    )
    try:
        with caplog.at_level("WARNING"), TestClient(api_module.app) as c:
            api_module.state.stop()
            assert c.get("/health").status_code == 200
        warning = next(r.getMessage() for r in caplog.records if "ancien format" in r.getMessage())
        assert "alice" in warning and "bob" not in warning and "carol" not in warning  # seul l'ancien format est signalé
    finally:
        api_module.state.storage.dispose()


def test_the_role_is_read_at_every_request_not_frozen_in_the_token(roles_client, monkeypatch):
    bob, carol = _bearer(roles_client, "bob"), _bearer(roles_client, "carol")
    assert roles_client.delete("/maintenance/nope", headers=bob).status_code == 404  # opérateur : passe l'autorisation
    h = auth.hash_password(PW)
    monkeypatch.setenv("NM_USERS", json.dumps({"bob": {"hash": h, "role": "viewer"}}))  # bob rétrogradé, carol retirée
    assert roles_client.delete("/maintenance/nope", headers=bob).status_code == 403  # effet immédiat, même jeton
    assert roles_client.get("/notifications", headers=bob).status_code == 200
    gone = roles_client.get("/notifications", headers=carol)
    assert gone.status_code == 401 and "n'existe plus" in gone.json()["detail"]  # session d'un compte retiré


def test_the_machine_token_is_admin_unless_told_otherwise(roles_client, monkeypatch):
    machine = {"Authorization": "Bearer machine-token"}
    assert roles_client.post("/notifications/test/nope", headers=machine).status_code == 404  # admin : comme avant les rôles
    monkeypatch.setenv("NM_API_TOKEN_ROLE", "viewer")
    assert roles_client.get("/notifications", headers=machine).status_code == 200
    assert roles_client.put("/incidents/nope/ack", headers=machine).status_code == 403


@pytest.mark.parametrize(
    "users,fragment",
    [
        ("pas du json", "JSON valide"),
        ('["alice"]', "objet JSON"),
        (json.dumps({"bob": {"hash": "x"}}), "rôle obligatoire"),  # le nouveau format n'accorde jamais un rôle par défaut
        (json.dumps({"bob": {"hash": "x", "role": "boss"}}), "rôle inconnu"),
        (json.dumps({"bob": {"hash": "x", "role": "admin", "extra": 1}}), "rôle obligatoire"),
        (json.dumps({"bob": {"role": "admin"}}), "rôle obligatoire"),
        (json.dumps({"bob": 12}), "rôle obligatoire"),
        (json.dumps({"api-token": "hash"}), "réservé"),  # se confondrait avec le jeton machine
    ],
)
def test_malformed_credentials_configuration_is_refused_with_a_clear_message(monkeypatch, users, fragment):
    monkeypatch.setenv("NM_USERS", users)
    with pytest.raises(auth.AuthConfigError, match=fragment):
        auth.load_settings()


def test_unknown_machine_role_is_refused(monkeypatch):
    monkeypatch.setenv("NM_API_TOKEN_ROLE", "root")
    with pytest.raises(auth.AuthConfigError, match="NM_API_TOKEN_ROLE"):
        auth.load_settings()


def test_the_service_refuses_to_start_with_a_malformed_users_configuration(client, monkeypatch):
    monkeypatch.setenv("NM_USERS", json.dumps({"bob": {"hash": "x", "role": "boss"}}))
    with pytest.raises(auth.AuthConfigError, match="rôle inconnu"), TestClient(api_module.app):
        pass
