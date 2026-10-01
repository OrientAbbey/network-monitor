"""Tests de l'API FastAPI (§37)."""

import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.app.config import AppConfig
from network_monitor.app.models import Severity
from network_monitor.data.mock import AnomalyScenario, ScenarioType


def _wait_until(predicate, timeout: float = 30.0, interval: float = 0.05):
    """Attend (jusqu'à `timeout`) qu'une condition soit vraie et renvoie sa dernière valeur. Remplace les sleep fixes, qui dépendent de la vitesse de la machine."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


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
    state.storage.dispose()


def state_running_and_calibrated(client) -> bool:
    body = client.get("/monitoring/status").json()
    return bool(body["running"] and body["calibrated"])


def test_health_ok_while_the_loop_is_ticking(client):
    _wait_until(lambda: client.get("/health").status_code == 200)
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["problems"] == []
    assert body["seconds_since_last_tick"] is not None


def test_monitoring_starts_automatically_via_lifespan(client):
    r = client.get("/monitoring/status")
    body = r.json()
    assert body["running"] is True
    assert body["calibrated"] is True
    assert set(body["outputs"]) == {"OUT_001", "OUT_002", "OUT_003"}


def test_outputs_listing_reflects_live_data(client):
    # attendre que chaque sortie ait reçu au moins une mesure
    _wait_until(lambda: all(o["latest_sample"] is not None for o in client.get("/outputs").json()))
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
    _wait_until(lambda: client.get("/outputs/OUT_001/history").json()
                and client.get("/outputs/OUT_001/anomalies").json())
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

    def _matches():
        incidents = client.get("/incidents", params={"limit": 200}).json()
        return [
            i for i in incidents
            if i["output_id"] == "OUT_001" and i["incident_type"] == "traffic_drop_with_high_loss"
        ]

    matches = _wait_until(_matches)
    assert matches, "le scénario scripté aurait dû produire au moins un incident traffic_drop_with_high_loss"

    detail = client.get(f"/incidents/{matches[0]['id']}").json()
    assert detail["id"] == matches[0]["id"]
    assert len(detail["evidence"]) > 0

    # La chronologie des alertes (§27) doit être exposée : au moins SUSPECT puis OPENED.
    alerts = _wait_until(lambda: client.get(f"/incidents/{matches[0]['id']}/alerts").json()
                         and {"suspect", "opened"} <= {a["event"] for a in
                                                       client.get(f"/incidents/{matches[0]['id']}/alerts").json()}
                         and client.get(f"/incidents/{matches[0]['id']}/alerts").json())
    events = [a["event"] for a in reversed(alerts)]  # ordre chronologique
    assert events.index("suspect") < events.index("opened")
    assert all(a["incident_id"] == matches[0]["id"] and a["output_id"] == "OUT_001" for a in alerts)

    by_output = client.get("/outputs/OUT_001/alerts", params={"limit": 500}).json()
    assert {a["incident_id"] for a in alerts} <= {a["incident_id"] for a in by_output}


def test_unknown_incident_returns_404(client):
    assert client.get("/incidents/does-not-exist").status_code == 404


def test_forecast_ahead_returns_points_beyond_now(client):
    _wait_until(lambda: state_running_and_calibrated(client))
    r = client.get("/outputs/OUT_001/forecast/ahead", params={"horizon_minutes": 30})
    assert r.status_code == 200
    points = r.json()
    assert len(points) == 6  # 30 min / 5 min par pas
    assert all(p["lower_bound_mbps"] <= p["predicted_traffic_mbps"] <= p["upper_bound_mbps"] for p in points)


def test_forecast_ahead_unknown_output_returns_404(client):
    assert client.get("/outputs/UNKNOWN/forecast/ahead").status_code == 404


# --- Sécurité et robustesse des entrées ---


def test_cors_allows_local_frontend_origin(client):
    r = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_rejects_foreign_origin(client):
    r = client.get("/health", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight_refused_for_foreign_origin(client):
    r = client.options(
        "/config",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert "access-control-allow-origin" not in r.headers


def test_history_window_too_wide_is_rejected(client):
    r = client.get(
        "/outputs/OUT_001/history",
        params={"since": "2020-01-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 400


def test_history_accepts_naive_datetimes_without_server_error(client):
    r = client.get(
        "/outputs/OUT_001/history",
        params={"since": "2026-01-01T00:00:00", "until": "2026-01-02T00:00:00"},
    )
    assert r.status_code == 200


def test_config_rejects_warning_score_above_open_score(client):
    r = client.put("/config", json={"warning_score": 0.9})
    assert r.status_code == 422
    assert client.get("/config").json()["incident"]["warning_score"] == 0.5


def _loop_threads():
    return [t for t in threading.enumerate() if t.name == "monitoring-loop" and t.is_alive()]


def test_stop_then_start_never_runs_two_loop_threads(client):
    for _ in range(3):
        client.post("/monitoring/stop")
        assert _loop_threads() == []  # stop() attend la fin du thread
        client.post("/monitoring/start")
        assert len(_loop_threads()) == 1


def test_alerts_endpoints_404_and_empty(client):
    assert client.get("/incidents/inconnu/alerts").status_code == 404
    assert client.get("/outputs/NOPE/alerts").status_code == 404
    assert client.get("/outputs/OUT_003/alerts").status_code == 200


# --- Instantanés (remplacent 24 requêtes du tableau comparatif par 1 par sortie) -----


def test_snapshots_default_offsets_and_content(client):
    _wait_until(lambda: len(client.get("/outputs/OUT_001/history").json()) >= 4)
    body = client.get("/outputs/OUT_001/snapshots").json()
    assert [s["offset_seconds"] for s in body] == [0, 300, 86400, 604800]
    now, prev5 = body[0], body[1]
    assert now["current"] is not None and now["previous"] is not None
    # « Actuelle » et « Précédente » doivent être deux mesures DIFFÉRENTES, consécutives.
    assert prev5["current"]["timestamp"] == now["previous"]["timestamp"]
    assert now["current"]["timestamp"] > prev5["current"]["timestamp"]
    # La référence par défaut est la dernière mesure connue de la sortie.
    latest = next(o for o in client.get("/outputs").json() if o["output_id"] == "OUT_001")["latest_sample"]
    # (le snapshot est pris AVANT /outputs et la simulation avance entre les deux : au plus 2 pas d'écart)
    snap_ts = datetime.fromisoformat(now["current"]["timestamp"].replace("Z", "+00:00"))
    latest_ts = datetime.fromisoformat(latest["timestamp"].replace("Z", "+00:00"))
    assert timedelta(0) <= latest_ts - snap_ts <= timedelta(minutes=10)
    # Le simulateur ne tourne que depuis quelques secondes : rien d'il y a une semaine.
    assert body[3]["current"] is None and body[3]["previous"] is None


def test_snapshots_match_history_nearest_point(client):
    _wait_until(lambda: len(client.get("/outputs/OUT_002/history").json()) >= 3)
    at = api_module.state.now
    snap = client.get("/outputs/OUT_002/snapshots", params={"offsets": [0], "at": at.isoformat()}).json()[0]
    hist = client.get(
        "/outputs/OUT_002/history",
        params={"since": (at - timedelta(minutes=35)).isoformat(), "until": (at + timedelta(minutes=10)).isoformat()},
    ).json()
    expected = min(hist, key=lambda p: abs(datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")) - at))
    assert snap["current"]["timestamp"] == expected["timestamp"]


@pytest.mark.parametrize(
    "params",
    [{"offsets": [-1]}, {"offsets": [40 * 86400]}, {"offsets": list(range(9))}],
)
def test_snapshots_reject_invalid_offsets(client, params):
    assert client.get("/outputs/OUT_001/snapshots", params=params).status_code == 400


def test_snapshots_unknown_output_is_404(client):
    assert client.get("/outputs/NOPE/snapshots").status_code == 404


# --- /health reflète l'état réel de la boucle --------------------------------


def _health_problems(client):
    r = client.get("/health")
    assert r.status_code == 503, r.json()
    assert r.json()["status"] == "unhealthy"
    return " | ".join(r.json()["problems"])


def test_health_is_503_when_monitoring_is_stopped(client):
    client.post("/monitoring/stop")
    assert "arrêtée" in _health_problems(client)
    client.post("/monitoring/start")
    assert _wait_until(lambda: client.get("/health").status_code == 200)


def test_health_is_503_when_no_tick_succeeds_for_too_long(client):
    _wait_until(lambda: client.get("/health").status_code == 200)
    api_module.state._last_tick_ok = time.monotonic() - 10_000
    assert "aucun tick réussi" in _health_problems(client)


def test_health_is_503_after_consecutive_tick_failures_then_recovers(client, monkeypatch):
    _wait_until(lambda: client.get("/health").status_code == 200)

    def boom(now):
        raise RuntimeError("source indisponible")

    monkeypatch.setattr(api_module.state.loop, "run_tick", boom)
    assert _wait_until(lambda: api_module.state.consecutive_failures >= api_module.HEALTH_MAX_FAILURES)
    assert "échecs de tick consécutifs" in _health_problems(client)

    monkeypatch.undo()
    assert _wait_until(lambda: client.get("/health").status_code == 200)


def test_health_is_503_when_the_loop_thread_died(client):
    _wait_until(lambda: client.get("/health").status_code == 200)
    thread = api_module.state._thread
    assert thread is not None
    api_module.state._stop.set()
    thread.join(timeout=5)
    assert "thread de monitoring mort" in _health_problems(client)


# --- /incidents : liste sans preuves, détail complet --------------------------


def _wait_for_incident(client):
    def _find():
        incidents = client.get("/incidents", params={"limit": 200}).json()
        return next((i for i in incidents if i["output_id"] == "OUT_001" and i["status"] in ("open", "stale")), None)

    return _wait_until(_find)


def _inject_long_incident():
    state = api_module.state
    start = state.now + timedelta(minutes=5)
    state.source.add_scenario(
        AnomalyScenario(
            scenario_id="LONG", output_id="OUT_001", start_time=start, end_time=start + timedelta(hours=6),
            type=ScenarioType.TRAFFIC_DROP_HIGH_LOSS, severity=Severity.CRITICAL,
            traffic_drop_fraction=0.7, loss_offset_points=10.0,
        )
    )


def test_incident_list_omits_evidence_but_detail_has_it(client):
    _inject_long_incident()
    summary = _wait_for_incident(client)
    assert summary is not None
    assert "evidence" not in summary
    detail = _wait_until(lambda: (lambda d: d if len(d["evidence"]) >= 3 else None)(client.get(f"/incidents/{summary['id']}").json()))
    assert detail is not None
    assert detail["id"] == summary["id"] and len(detail["evidence"]) >= 3


def test_incident_summary_has_every_incident_field_except_evidence():
    from network_monitor.app.models import Incident

    assert set(api_module.IncidentSummary.model_fields) == set(Incident.model_fields) - {"evidence"}


def test_outputs_still_report_the_active_incident_without_loading_evidence(client):
    _inject_long_incident()
    summary = _wait_for_incident(client)
    outputs = _wait_until(lambda: (lambda o: o if o["active_incident_id"] else None)(
        next(o for o in client.get("/outputs").json() if o["output_id"] == "OUT_001")))
    assert outputs["active_incident_id"] == summary["id"]
