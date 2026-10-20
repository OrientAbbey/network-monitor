"""Tests de l'API FastAPI (§37)."""

import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.app.config import AppConfig
from network_monitor.app.models import Severity
from network_monitor.data.mock import AnomalyScenario, ScenarioType

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NM_API_TOKEN", "test-token")
    db_url = f"sqlite:///{tmp_path / 'test.db'}"
    # Historique court (< model_selection.minimum_history) : reste sur la
    # Seasonal Baseline, évite le coût de Holt-Winters à chaque test.
    config = AppConfig(history={"minimum": "30d", "preferred": "40d"})
    state = api_module.MonitoringState(config=config, db_url=db_url)
    state.seconds_per_tick = 0.05  # accélérer très fortement pour les tests
    api_module.state = state
    with TestClient(api_module.app) as c:
        yield c
    state.stop()
    state.storage.dispose()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_monitoring_starts_automatically_via_lifespan(client):
    r = client.get("/monitoring/status")
    body = r.json()
    assert body["running"] is True
    assert body["calibrated"] is True
    assert set(body["outputs"]) == {"OUT_001", "OUT_002", "OUT_003"}


def test_outputs_listing_reflects_live_data(client):
    _wait_until(lambda: all(o["latest_sample"] for o in client.get("/outputs").json()))
    r = client.get("/outputs")
    assert r.status_code == 200
    body = r.json()
    assert {o["output_id"] for o in body} == {"OUT_001", "OUT_002", "OUT_003"}
    assert all(o["latest_sample"] is not None for o in body)


def test_unknown_output_returns_404(client):
    assert client.get("/outputs/UNKNOWN/history").status_code == 404
    assert client.get("/outputs/UNKNOWN/anomalies").status_code == 404
    assert client.get("/outputs/UNKNOWN/forecast").status_code == 404


def test_history_window_validation(client):
    r = client.get("/outputs/OUT_001/history", params={"since": "2030-01-01T00:00:00Z", "until": "2020-01-01T00:00:00Z"})
    assert r.status_code == 400


def test_history_and_anomalies_grow_over_time(client):
    _wait_until(lambda: len(client.get("/outputs/OUT_001/anomalies").json()) > 0)
    hist = client.get("/outputs/OUT_001/history").json()
    anomalies = client.get("/outputs/OUT_001/anomalies").json()
    assert len(hist) > 0
    assert len(anomalies) > 0


def test_config_roundtrip(client):
    cfg = client.get("/config").json()
    assert cfg["incident"]["open_score"] == 0.75

    r = client.put("/config", json={"open_score": 0.6}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["incident"]["open_score"] == 0.6

    # confirmé par une relecture, pas seulement par la réponse de l'écriture
    assert client.get("/config").json()["incident"]["open_score"] == 0.6

    # la configuration par défaut partagée entre instances n'a pas bougé
    assert api_module.DEFAULT_API_CONFIG.incident.open_score == 0.75


def test_config_rejects_incoherent_thresholds(client):
    r = client.put("/config", json={"open_score": 0.5, "close_score": 0.6}, headers=AUTH)
    assert r.status_code == 422
    # la configuration précédente doit rester en place (aucun effet partiel)
    assert client.get("/config").json()["incident"]["open_score"] == 0.75


def test_config_rejects_warning_score_above_open_score(client):
    r = client.put("/config", json={"open_score": 0.7, "warning_score": 0.9}, headers=AUTH)
    assert r.status_code == 422
    assert client.get("/config").json()["incident"]["open_score"] == 0.75


def test_config_validation_errors_are_structured_and_do_not_echo_input(client):
    r = client.put("/config", json={"open_score": 0.7, "warning_score": 0.9}, headers=AUTH)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list) and all(set(e) <= {"type", "loc", "msg"} for e in detail)  # pas de `input`, pas de `url`
    assert "warning_score" in detail[0]["msg"] and "0.9" not in str(detail[0].get("loc", ""))


def test_control_endpoints_require_authentication(client):
    assert client.put("/config", json={"open_score": 0.6}).status_code == 401
    assert client.post("/monitoring/start").status_code == 401
    assert client.post("/monitoring/stop").status_code == 401


def test_stop_and_start_monitoring(client):
    r = client.post("/monitoring/stop", headers=AUTH)
    assert r.json()["running"] is False
    r = client.post("/monitoring/start", headers=AUTH)
    assert r.json()["running"] is True


def test_scripted_incident_is_visible_end_to_end(client):
    """Un scénario injecté doit être détecté et exposé via /incidents et /incidents/{id}."""
    state = api_module.state
    start = state.now + timedelta(minutes=5)
    end = start + timedelta(minutes=40)
    state.source.add_scenario(
        AnomalyScenario(
            scenario_id="TEST",
            output_id="OUT_001",
            start_time=start,
            end_time=end,
            type=ScenarioType.TRAFFIC_DROP_HIGH_LOSS,
            severity=Severity.CRITICAL,
            traffic_drop_fraction=0.7,
            loss_offset_points=10.0,
        )
    )
    def _matches():
        incidents = client.get("/incidents", params={"limit": 200}).json()
        return [i for i in incidents if i["output_id"] == "OUT_001" and i["incident_type"] == "traffic_drop_with_high_loss"]

    _wait_until(lambda: bool(_matches()), timeout=30.0)
    matches = _matches()
    assert matches, "le scénario scripté aurait dû produire au moins un incident traffic_drop_with_high_loss"

    detail = client.get(f"/incidents/{matches[0]['id']}").json()
    assert detail["id"] == matches[0]["id"]
    assert len(detail["evidence"]) > 0


def test_unknown_incident_returns_404(client):
    assert client.get("/incidents/does-not-exist").status_code == 404


def test_forecast_ahead_returns_points_beyond_now(client):
    _wait_until(lambda: client.get("/monitoring/status").json()["calibrated"])
    r = client.get("/outputs/OUT_001/forecast/ahead", params={"horizon_minutes": 30})
    assert r.status_code == 200
    points = r.json()
    assert len(points) == 6  # 30 min / 5 min par pas
    assert all(p["lower_bound_mbps"] <= p["predicted_traffic_mbps"] <= p["upper_bound_mbps"] for p in points)


def test_forecast_ahead_unknown_output_returns_404(client):
    assert client.get("/outputs/UNKNOWN/forecast/ahead").status_code == 404


def test_naive_datetime_is_rejected_not_a_500(client):
    oid = client.get("/outputs").json()[0]["output_id"]
    r = client.get(f"/outputs/{oid}/history", params={"since": "2026-01-01T00:00:00"})
    assert r.status_code == 422


def test_window_is_bounded(client):
    oid = client.get("/outputs").json()[0]["output_id"]
    r = client.get(f"/outputs/{oid}/history", params={"since": "2020-01-01T00:00:00Z"})
    assert r.status_code == 400


def test_span_hours_is_relative_to_server_clock(client):
    """La fenêtre relative suit l'horloge simulée du serveur, pas celle du client (§13)."""
    oid = client.get("/outputs").json()[0]["output_id"]
    _wait_until(lambda: len(client.get(f"/outputs/{oid}/history", params={"span_hours": 12}).json()) >= 3)
    status = client.get("/monitoring/status").json()
    assert "server_time" in status and "simulated_now" in status


def test_alerts_endpoint_lists_persisted_alerts(client):
    r = client.get("/alerts")
    assert r.status_code == 200 and isinstance(r.json(), list)
    assert client.get("/alerts", params={"output_id": "inconnue"}).status_code == 404


def test_nearest_returns_what_history_would_in_a_single_request(client):
    # Le stockage ne contient que ce qui a été mesuré depuis le démarrage (ARCHITECTURE §11) : on attend d'avoir ~70 min de
    # mesures simulées pour que 30 et 45 min en arrière tombent au milieu des données, avec des échantillons des deux côtés
    # de la cible. Alors seulement « le plus proche » se distingue de « le dernier » (à 0 et 5 min, les données s'arrêtent là).
    _wait_until(lambda: all(len(client.get(f"/outputs/{o}/history", params={"span_hours": 3}).json()) >= 14 for o in ("OUT_001", "OUT_002", "OUT_003")))
    state = api_module.state
    state.stop()  # fige l'horloge : sinon `state.now` avance entre les deux lectures comparées
    r = client.get("/outputs/nearest", params=[("ago_minutes", m) for m in (0, 5, 5, 30, 45)])  # le doublon est toléré
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"OUT_001", "OUT_002", "OUT_003"}
    for output_id, by_ago in body.items():
        assert set(by_ago) == {"0", "5", "30", "45"}
        for ago, sample in by_ago.items():
            target = state.now - timedelta(minutes=int(ago))
            window = client.get(
                f"/outputs/{output_id}/history",
                params={"since": (target - timedelta(minutes=35)).isoformat(), "until": (target + timedelta(minutes=10)).isoformat()},
            ).json()
            assert window and sample is not None
            expected = min(window, key=lambda x: abs(datetime.fromisoformat(x["timestamp"]) - target))
            assert sample == expected  # le même échantillon, avec les mêmes champs, que le calcul fait jusqu'ici dans le navigateur
            if ago in ("30", "45"):
                assert any(datetime.fromisoformat(x["timestamp"]) > datetime.fromisoformat(sample["timestamp"]) for x in window)  # des mesures plus tardives existent


def test_nearest_searches_the_documented_window_and_says_null_when_empty(client, monkeypatch):
    _wait_until(lambda: all(o["latest_sample"] for o in client.get("/outputs").json()))
    state = api_module.state
    state.stop()
    asked = []

    def nothing(output_id, start, end):
        asked.append((output_id, start, end))
        return []

    monkeypatch.setattr(state.storage, "get_history", nothing)
    body = client.get("/outputs/nearest", params={"ago_minutes": 90}).json()
    assert all(by_ago == {"90": None} for by_ago in body.values())  # pas de mesure : null, jamais une valeur inventée
    target = state.now - timedelta(minutes=90)
    assert {(o, s, e) for o, s, e in asked} == {(o, target - timedelta(minutes=35), target + timedelta(minutes=10)) for o in body}


def test_nearest_picks_the_closest_sample_not_the_first_or_the_last(client, monkeypatch):
    _wait_until(lambda: all(o["latest_sample"] for o in client.get("/outputs").json()))
    state = api_module.state
    state.stop()
    target = state.now - timedelta(minutes=60)
    base = state.storage.get_history("OUT_001", state.now - timedelta(hours=1), state.now)[0]
    around = [base.model_copy(update={"timestamp": target + timedelta(minutes=m)}) for m in (-30, -2, 8)]
    monkeypatch.setattr(state.storage, "get_history", lambda output_id, start, end: around)
    got = client.get("/outputs/nearest", params={"ago_minutes": 60}).json()["OUT_001"]["60"]
    assert datetime.fromisoformat(got["timestamp"]) == target - timedelta(minutes=2)


@pytest.mark.parametrize(
    "params",
    [
        {},  # au moins un instant
        {"ago_minutes": -1},
        {"ago_minutes": 31 * 24 * 60 + 1},  # au-delà de la plage maximale autorisée partout ailleurs
        {"ago_minutes": "hier"},
        [("ago_minutes", m) for m in range(17)],  # au plus 16 instants
    ],
)
def test_nearest_rejects_invalid_requests(client, params):
    assert client.get("/outputs/nearest", params=params).status_code == 422


def test_nearest_accepts_the_maximum_size_and_range(client):
    params = [("ago_minutes", m) for m in range(15)] + [("ago_minutes", 31 * 24 * 60)]
    assert client.get("/outputs/nearest", params=params).status_code == 200


def _wait_until(condition, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError("condition non remplie avant le délai maximal")


def test_alerts_endpoint_hides_weak_signals_unless_asked(client):
    from datetime import datetime, timezone

    from network_monitor.app.models import IncidentType
    from network_monitor.incidents.manager import Alert

    t = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def alert(event, confirmed, minute):
        return Alert(
            incident_id=f"i{minute}", output_id="OUT_001", severity=Severity.INFO if not confirmed else Severity.CRITICAL,
            event=event, timestamp=t + timedelta(minutes=minute), message="m",
            incident_type=IncidentType.TRAFFIC_DROP,
            confirmed=confirmed,
        )

    api_module.state.storage.save_alerts([
        alert("suspect", False, 0), alert("closed", False, 5),        # bruit résorbé sans confirmation
        alert("suspect", True, 10), alert("recovering", True, 12),    # états intermédiaires d'un vrai incident
        alert("opened", True, 15), alert("closed", True, 50),         # ce qui compte
    ])
    kept = client.get("/alerts").json()
    assert sorted(a["event"] for a in kept) == ["closed", "opened"] and all(a["incident_id"] in ("i15", "i50") for a in kept)
    assert len(client.get("/alerts", params={"all": "true"}).json()) == 6


def test_mock_source_in_production_logs_a_warning(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(api_module, "_is_development", False)
    state = api_module.MonitoringState(
        config=AppConfig.model_validate({"history": {"minimum": "30d", "preferred": "40d"}}),
        db_url=f"sqlite:///{tmp_path / 'w.db'}",
    )
    api_module.state = state
    with caplog.at_level(logging.WARNING, logger=api_module.logger.name), TestClient(api_module.app):
        state.stop()
    state.storage.dispose()
    assert any("SIMULÉES" in r.message for r in caplog.records)
