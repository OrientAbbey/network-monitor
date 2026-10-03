"""Rapport de disponibilité (§33) : union des intervalles, rétro-datage, couverture, API et CSV."""

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import network_monitor.app.api as api_module
from network_monitor.analysis.availability import build_report
from network_monitor.app.config import AppConfig
from network_monitor.app.models import Incident, IncidentStatus, IncidentType, Severity

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
H = timedelta(hours=1)


def _inc(output="O", start=0.0, end=1.0, opened=None, severity=Severity.CRITICAL, confirmed=True, active=False, ident="i"):
    """Incident de `start` à `end` (en heures après T0), confirmé `opened` heures après T0."""
    opened_at = T0 + (opened if opened is not None else start + 0.25) * H
    return Incident(
        id=ident, output_id=output, status=IncidentStatus.OPEN if active else IncidentStatus.CLOSED, severity=severity,
        incident_type=IncidentType.TRAFFIC_DROP, suspected_at=T0 + start * H,
        opened_at=opened_at if confirmed else None, started_at=T0 + start * H if confirmed else None,
        ended_at=None if active else T0 + end * H, closed_at=None if active else T0 + (end + 0.1) * H,
        last_seen_at=T0 + end * H, score=0.9,
    )


def _report(incidents, hours=24, now_hours=None, counts=None, **kw):
    now = T0 + (now_hours if now_hours is not None else hours) * H
    return build_report(incidents, ["O"], T0, T0 + hours * H, now, STEP, counts if counts is not None else {"O": hours * 12}, **kw).outputs[0]


def test_availability_uses_retro_dated_start_and_end():
    r = _report([_inc(start=2, end=3, opened=2.5)])  # début réel 2 h, confirmé à 2 h 30, fin réelle 3 h
    assert r.downtime_seconds == 3600  # 1 h, pas 30 min (confirmation → fermeture)
    assert r.availability_percent == pytest.approx(100 * (1 - 1 / 24), abs=1e-3)
    assert r.mttd_seconds == 1800 and r.mttr_seconds == 3600 and r.incidents == 1


def test_overlapping_incidents_are_counted_once():
    r = _report([_inc(start=1, end=3, ident="a"), _inc(start=2, end=4, ident="b"), _inc(start=10, end=11, ident="c")])
    assert r.downtime_seconds == (3 + 1) * 3600  # union : [1, 4[ + [10, 11[
    assert r.incidents == 3 and r.longest_incident_seconds == 2 * 3600


def test_degradation_is_separate_from_downtime_and_never_double_counted():
    r = _report([_inc(start=1, end=3, severity=Severity.CRITICAL, ident="a"), _inc(start=2, end=5, severity=Severity.WARNING, ident="b")])
    assert r.downtime_seconds == 2 * 3600 and r.degraded_seconds == 2 * 3600  # le WARNING hors de [1, 3[ : [3, 5[
    assert _report([_inc(severity=Severity.WARNING)], downtime_severity=Severity.WARNING).downtime_seconds == 3600


def test_incidents_are_clipped_to_the_period_and_open_ones_run_until_now():
    clipped = _report([_inc(start=-2, end=1)])  # commencé avant la période
    assert clipped.downtime_seconds == 3600
    still_open = _report([_inc(start=10, end=10.5, active=True)], now_hours=14)
    assert still_open.downtime_seconds == 4 * 3600  # toujours en cours à 14 h
    # la période n'est pas finie : la disponibilité se rapporte au temps écoulé, pas aux 24 h entières
    assert still_open.availability_percent == pytest.approx(100 * (1 - 4 / 14), abs=1e-3)


def test_unconfirmed_incidents_do_not_count():
    r = _report([_inc(confirmed=False)])
    assert r.incidents == 0 and r.downtime_seconds == 0 and r.availability_percent == 100.0
    assert r.mttd_seconds is None and r.mttr_seconds is None


def test_coverage_reflects_missing_measurements_and_retention():
    assert _report([], counts={"O": 288}).coverage_percent == 100.0  # 24 h × 12 mesures par heure
    assert _report([], counts={"O": 144}).coverage_percent == 50.0
    assert _report([], counts={}).coverage_percent == 0.0
    # mesures brutes purgées avant `coverage_start` : on ne juge que la part conservée (12 h ici)
    kept = build_report([], ["O"], T0, T0 + 24 * H, T0 + 24 * H, STEP, {"O": 144}, coverage_start=T0 + 12 * H).outputs[0]
    assert kept.coverage_percent == 100.0


def test_report_rejects_an_empty_period_and_exports_csv():
    with pytest.raises(ValueError):
        build_report([], ["O"], T0, T0, T0, STEP, {})
    rows = list(csv.DictReader(io.StringIO(build_report([_inc(start=1, end=2)], ["O"], T0, T0 + 24 * H, T0 + 24 * H, STEP, {"O": 288}).to_csv())))
    assert rows[0]["output_id"] == "O" and float(rows[0]["downtime_seconds"]) == 3600 and rows[0]["availability_percent"] != ""


# -- API ------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    state = api_module.MonitoringState(
        config=AppConfig.model_validate({"history": {"minimum": "30d", "preferred": "40d"}}),
        db_url=f"sqlite:///{tmp_path / 'r.db'}",
    )
    state.now = T0 + 24 * H
    api_module.state = state
    state.storage.upsert_incident(_inc(output="OUT_001", start=2, end=3))
    state.storage.upsert_incident(_inc(output="OUT_001", start=2.5, end=4, ident="j"))
    state.storage.upsert_incident(_inc(output="OUT_002", start=5, end=5.5, severity=Severity.WARNING, ident="k"))
    state.storage.upsert_incident(_inc(output="OUT_003", confirmed=False, ident="n").model_copy(update={"opened_at": None}))
    with TestClient(api_module.app) as c:
        state.stop()
        yield c
    state.storage.dispose()


def _window(**extra):
    return {"start": T0.isoformat(), "end": (T0 + 24 * H).isoformat()} | extra


def test_api_report_json(client):
    r = client.get("/reports/availability", params=_window())
    assert r.status_code == 200
    by = {o["output_id"]: o for o in r.json()["outputs"]}
    assert set(by) == {"OUT_001", "OUT_002", "OUT_003"}
    assert by["OUT_001"]["downtime_seconds"] == 2 * 3600  # union de [2, 3[ et [2,5, 4[ = [2, 4[
    assert by["OUT_002"]["downtime_seconds"] == 0 and by["OUT_002"]["degraded_seconds"] == 1800
    assert by["OUT_003"]["incidents"] == 0 and by["OUT_003"]["availability_percent"] == 100.0


def test_api_report_filters_by_output_and_exports_csv(client):
    r = client.get("/reports/availability", params=_window(output_id="OUT_001", format="csv"))
    assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["output_id"] for row in rows] == ["OUT_001"]


def test_api_report_validation(client):
    assert client.get("/reports/availability", params={"start": "2026-09-01T00:00:00"}).status_code == 422  # sans fuseau
    assert client.get("/reports/availability", params=_window(output_id="NOPE")).status_code == 404
    assert client.get("/reports/availability", params={"start": (T0 + 2 * H).isoformat(), "end": T0.isoformat()}).status_code == 400
    assert client.get("/reports/availability", params={"start": (T0 - timedelta(days=400)).isoformat()}).status_code == 400
    assert client.get("/reports/availability", params=_window(downtime_severity="nope")).status_code == 422
    assert client.get("/reports/availability").status_code == 200  # défaut : 30 jours se terminant maintenant
    assert client.get("/reports/availability", params={"days": 7, "start": T0.isoformat()}).status_code == 422
    assert client.get("/reports/availability", params={"days": 0}).status_code == 422


def test_api_days_is_relative_to_the_server_clock(client):
    """Le client n'envoie aucune date : la fenêtre se calcule sur l'horloge du serveur (ici T0 + 24 h)."""
    r = client.get("/reports/availability", params={"days": 1})
    assert r.status_code == 200
    assert r.json()["start"].startswith("2026-09-01T00:00:00") and r.json()["end"].startswith("2026-09-02T00:00:00")
    out = {o["output_id"]: o for o in r.json()["outputs"]}
    assert out["OUT_001"]["downtime_seconds"] == 2 * 3600
