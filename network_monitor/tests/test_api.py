"""Tests de l'API FastAPI (§37)."""

import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.app.config import AppConfig
from network_monitor.app.models import Severity
from network_monitor.data.mock import AnomalyScenario, ScenarioType


@pytest.fixture
def client(tmp_path):
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
    time.sleep(1.0)  # laisser tourner quelques ticks
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
    time.sleep(1.0)
    hist = client.get("/outputs/OUT_001/history").json()
    anomalies = client.get("/outputs/OUT_001/anomalies").json()
    assert len(hist) > 0
    assert len(anomalies) > 0


def test_config_roundtrip(client):
    cfg = client.get("/config").json()
    assert cfg["incident"]["open_score"] == 0.75

    r = client.put("/config", json={"open_score": 0.6})
    assert r.status_code == 200
    assert r.json()["incident"]["open_score"] == 0.6

    # confirmé par une relecture, pas seulement par la réponse de l'écriture
    assert client.get("/config").json()["incident"]["open_score"] == 0.6


def test_config_rejects_incoherent_thresholds(client):
    r = client.put("/config", json={"open_score": 0.5, "close_score": 0.6})
    assert r.status_code == 422
    # la configuration précédente doit rester en place (aucun effet partiel)
    assert client.get("/config").json()["incident"]["open_score"] == 0.75


def test_stop_and_start_monitoring(client):
    r = client.post("/monitoring/stop")
    assert r.json()["running"] is False
    r = client.post("/monitoring/start")
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
    time.sleep(2.5)

    incidents = client.get("/incidents", params={"limit": 200}).json()
    matches = [i for i in incidents if i["output_id"] == "OUT_001" and i["incident_type"] == "traffic_drop_with_high_loss"]
    assert matches, "le scénario scripté aurait dû produire au moins un incident traffic_drop_with_high_loss"

    detail = client.get(f"/incidents/{matches[0]['id']}").json()
    assert detail["id"] == matches[0]["id"]
    assert len(detail["evidence"]) > 0


def test_unknown_incident_returns_404(client):
    assert client.get("/incidents/does-not-exist").status_code == 404


def test_forecast_ahead_returns_points_beyond_now(client):
    time.sleep(0.5)
    r = client.get("/outputs/OUT_001/forecast/ahead", params={"horizon_minutes": 30})
    assert r.status_code == 200
    points = r.json()
    assert len(points) == 6  # 30 min / 5 min par pas
    assert all(p["lower_bound_mbps"] <= p["predicted_traffic_mbps"] <= p["upper_bound_mbps"] for p in points)


def test_forecast_ahead_unknown_output_returns_404(client):
    assert client.get("/outputs/UNKNOWN/forecast/ahead").status_code == 404
