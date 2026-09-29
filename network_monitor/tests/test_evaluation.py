"""Évaluation quantitative (§33) : métriques sur données synthétiques (valeurs calculées à la main) + banc complet."""

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from network_monitor.app.models import (
    ForecastModelName,
    ForecastResult,
    Incident,
    IncidentStatus,
    IncidentType,
    Severity,
    TrafficSample,
)
from network_monitor.data.mock import AnomalyScenario, ScenarioType
from network_monitor.evaluation import (
    default_scenarios,
    false_positive_rate,
    forecast_metrics,
    match_incidents,
    run_evaluation,
    summarize_delays,
)
from network_monitor.evaluation.__main__ import main as cli_main

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
IV = timedelta(minutes=5)


def _sc(sid, out, start_min, end_min, typ=ScenarioType.TRAFFIC_DROP):
    return AnomalyScenario(
        scenario_id=sid, output_id=out, start_time=T0 + timedelta(minutes=start_min),
        end_time=T0 + timedelta(minutes=end_min), type=typ, severity=Severity.CRITICAL,
    )


def _inc(iid, out, open_min, close_min=None, confirmed=True):
    opened = T0 + timedelta(minutes=open_min) if confirmed else None
    closed = T0 + timedelta(minutes=close_min) if close_min is not None else None
    last = closed or (opened or T0) + timedelta(minutes=5)
    return Incident(
        id=iid, output_id=out,
        status=IncidentStatus.CLOSED if closed else IncidentStatus.OPEN,
        severity=Severity.WARNING, incident_type=IncidentType.TRAFFIC_DROP,
        suspected_at=T0 + timedelta(minutes=open_min - 5), opened_at=opened, last_seen_at=last,
        closed_at=closed, score=0.8, description="",
    )


# --- appariement, précision, rappel, F1 ---------------------------------------


def test_perfect_detection():
    res = match_incidents([_sc("a", "O1", 60, 100)], [_inc("i1", "O1", 70, 110)])
    assert res.recall == 1.0 and res.precision == 1.0 and res.f1 == 1.0
    (s,) = res.scenarios
    assert s.detected and s.fragments == 1
    assert s.detection_delay == timedelta(minutes=10)
    assert s.closure_delay == timedelta(minutes=10)
    assert s.duration_error == timedelta(0)  # 40 min détectés pour 40 min réelles
    assert s.predicted_start == T0 + timedelta(minutes=70) and s.predicted_end == T0 + timedelta(minutes=110)
    assert s.true_start == T0 + timedelta(minutes=60) and s.true_end == T0 + timedelta(minutes=100)


def test_missed_scenario_and_false_positive():
    scenarios = [_sc("a", "O1", 60, 100), _sc("b", "O2", 60, 100)]
    incidents = [_inc("i1", "O1", 70, 110), _inc("fp", "O3", 200, 230)]
    res = match_incidents(scenarios, incidents)
    assert res.detected_count == 1 and res.recall == 0.5
    assert res.total_incidents == 2 and res.true_positive_incidents == 1 and res.precision == 0.5
    assert [i.id for i in res.false_positive_incidents] == ["fp"]
    assert res.f1 == pytest.approx(0.5)
    missed = next(s for s in res.scenarios if s.scenario_id == "b")
    assert not missed.detected and missed.detection_delay is None and missed.predicted_start is None


def test_incident_on_other_output_or_outside_window_does_not_match():
    res = match_incidents([_sc("a", "O1", 60, 100)], [_inc("x", "O2", 70, 90), _inc("y", "O1", 300, 320)])
    assert res.detected_count == 0 and res.precision == 0.0 and res.f1 is None


def test_unconfirmed_suspect_is_not_a_detection_nor_a_false_positive():
    res = match_incidents([_sc("a", "O1", 60, 100)], [_inc("s", "O1", 70, None, confirmed=False)])
    assert res.detected_count == 0 and res.total_incidents == 0 and res.precision is None


def test_fragmentation_counts_all_as_true_positives_and_uses_first_open_last_close():
    incs = [_inc("i1", "O1", 60, 70), _inc("i2", "O1", 80, 105)]
    (s,) = match_incidents([_sc("a", "O1", 55, 100)], incs).scenarios
    assert s.fragments == 2
    assert s.detection_delay == timedelta(minutes=5)          # premier incident
    assert s.closure_delay == timedelta(minutes=5)            # dernier incident
    res = match_incidents([_sc("a", "O1", 55, 100)], incs)
    assert res.precision == 1.0 and res.true_positive_incidents == 2


def test_still_open_incident_has_no_closure_delay():
    (s,) = match_incidents([_sc("a", "O1", 60, 100)], [_inc("i1", "O1", 70, None)]).scenarios
    assert s.detected and s.closure_delay is None


def test_no_scenarios_or_incidents_gives_undefined_metrics():
    res = match_incidents([], [])
    assert res.recall is None and res.precision is None and res.f1 is None


def test_summarize_delays():
    assert summarize_delays([]) == {"count": 0, "mean_s": None, "median_s": None, "max_s": None}
    out = summarize_delays([timedelta(minutes=m) for m in (5, 10, 30)])
    assert out["count"] == 3 and out["max_s"] == 1800 and out["median_s"] == 600
    assert out["mean_s"] == pytest.approx(900.0)  # (5 + 10 + 30) / 3 = 15 min


# --- taux de faux positifs ------------------------------------------------------


def test_false_positive_rate_counts_only_normal_steps_and_honours_grace():
    scenarios = [_sc("a", "O1", 0, 30)]
    # incident 20-60 : couvre la fin de l'anomalie (légitime jusqu'à 30 + grâce) puis 30 min de faux positif
    incidents = [_inc("i", "O1", 20, 60)]
    end = T0 + timedelta(minutes=100)
    no_grace = false_positive_rate(scenarios, incidents, ["O1"], T0, end, IV)
    with_grace = false_positive_rate(scenarios, incidents, ["O1"], T0, end, IV, grace=timedelta(minutes=10))
    # pas normaux sans grâce : minutes 35..95 -> 13 pas ; signalés : 35..60 -> 6 pas
    assert no_grace == pytest.approx(6 / 13)
    # avec grâce (fin à 40) : normaux 45..95 -> 11 pas ; signalés : 45..60 -> 4 pas
    assert with_grace == pytest.approx(4 / 11)


def test_false_positive_rate_zero_without_incidents_and_none_without_normal_steps():
    scenarios = [_sc("a", "O1", 0, 30)]
    assert false_positive_rate(scenarios, [], ["O1"], T0, T0 + timedelta(minutes=100), IV) == 0.0
    assert false_positive_rate(scenarios, [], ["O1"], T0, T0 + timedelta(minutes=30), IV) is None


# --- prévision --------------------------------------------------------------------


def _f(out, minute, pred, lo, hi, conf=0.8):
    return ForecastResult(
        output_id=out, timestamp=T0 + timedelta(minutes=minute), predicted_traffic_mbps=pred,
        lower_bound_mbps=lo, upper_bound_mbps=hi, confidence=conf, model_name=ForecastModelName.SEASONAL_BASELINE,
    )


def _s(out, minute, value):
    return TrafficSample(timestamp=T0 + timedelta(minutes=minute), output_id=out, traffic_mbps=value, loss_percent=0.1)


def test_forecast_metrics_exact_values():
    forecasts = [_f("O", 0, 100, 90, 110), _f("O", 5, 100, 90, 110), _f("O", 10, 100, 90, 110), _f("O", 15, 100, 90, 110)]
    samples = [_s("O", 0, 100), _s("O", 5, 110), _s("O", 10, 80), _s("O", 15, 100)]
    m = forecast_metrics(forecasts, samples)
    # erreurs : 0, -10, +20, 0
    assert m["n"] == 4
    assert m["mae"] == pytest.approx(7.5)
    assert m["rmse"] == pytest.approx(math.sqrt((0 + 100 + 400 + 0) / 4))
    assert m["smape_percent"] == pytest.approx(100 * (0 + 2 * 10 / 210 + 2 * 20 / 180 + 0) / 4)
    assert m["picp"] == pytest.approx(0.75)  # 100, 110 (borne incluse) et 100 dans [90, 110] ; 80 dehors
    assert m["nominal_coverage"] == pytest.approx(0.8)


def test_forecast_metrics_exclusion_windows_and_missing_observations():
    forecasts = [_f("O", 0, 100, 90, 110), _f("O", 5, 100, 90, 110), _f("O", 10, 100, 90, 110), _f("P", 0, 5, 4, 6)]
    samples = [_s("O", 0, 100), _s("O", 5, 10), _s("O", 10, 100)]  # pas d'observation pour P
    excl = [("O", (T0 + timedelta(minutes=5), T0 + timedelta(minutes=5)))]
    m = forecast_metrics(forecasts, samples, excl)
    assert m["n"] == 2 and m["mae"] == 0.0 and m["picp"] == 1.0
    assert forecast_metrics([], [])["n"] == 0
    assert forecast_metrics([], [])["mae"] is None


# --- banc complet ---------------------------------------------------------------------


def test_default_scenarios_cover_all_types_after_eval_start_without_overlap_per_output():
    scenarios = default_scenarios(T0)
    assert {s.type for s in scenarios} == set(ScenarioType)
    assert all(s.start_time >= T0 + timedelta(hours=3) for s in scenarios)
    for out in {s.output_id for s in scenarios}:
        mine = sorted((s for s in scenarios if s.output_id == out), key=lambda s: s.start_time)
        assert all(a.end_time <= b.start_time for a, b in zip(mine, mine[1:], strict=False))


def test_run_evaluation_rejects_scenarios_overlapping_calibration_period():
    early = _sc("early", "OUT_001", -60, -20)
    with pytest.raises(ValueError, match="calibration"):
        run_evaluation(eval_start=T0, scenarios=[early])
    late = _sc("late", "OUT_001", 60 * 24 * 3, 60 * 24 * 3 + 30)
    with pytest.raises(ValueError, match="fin de la période"):
        run_evaluation(eval_start=T0, eval_days=1, scenarios=[late])


@pytest.fixture(scope="module")
def small_report():
    scenarios = [
        _sc("big_drop", "OUT_001", 4 * 60, 4 * 60 + 60, ScenarioType.TRAFFIC_DROP_HIGH_LOSS),
        _sc("spike", "OUT_002", 8 * 60, 8 * 60 + 60, ScenarioType.TRAFFIC_SPIKE),
    ]
    return run_evaluation(seed=3, eval_days=0.5, scenarios=scenarios)


def test_run_evaluation_end_to_end_report(small_report):
    r = small_report
    m = r.incident_metrics
    assert m["scenarios"] == 2 and m["detected"] == 2 and m["recall"] == 1.0
    assert 0.0 <= m["false_positive_rate"] <= 0.1
    assert m["detection_delay"]["max_s"] <= 30 * 60
    ids = {s["scenario_id"] for s in r.scenarios}
    assert ids == {"big_drop", "spike"} and all(s["detected"] for s in r.scenarios)
    f = r.forecast_metrics["overall"]
    assert f["n"] > 100 and f["smape_percent"] < 20 and 0.6 < f["picp"] < 0.95
    assert set(r.forecast_metrics["per_output"]) == {"OUT_001", "OUT_002", "OUT_003"}
    json.dumps(r.to_dict())  # sérialisable
    text = r.format_text()
    assert "big_drop" in text and "précision" in text and "MANQUÉ" not in text


def test_evaluation_is_deterministic(small_report):
    scenarios = [
        _sc("big_drop", "OUT_001", 4 * 60, 4 * 60 + 60, ScenarioType.TRAFFIC_DROP_HIGH_LOSS),
        _sc("spike", "OUT_002", 8 * 60, 8 * 60 + 60, ScenarioType.TRAFFIC_SPIKE),
    ]
    again = run_evaluation(seed=3, eval_days=0.5, scenarios=scenarios)
    assert again.incident_metrics == small_report.incident_metrics
    assert again.forecast_metrics == small_report.forecast_metrics


def test_cli_writes_json_report(tmp_path, capsys, monkeypatch, small_report):
    import network_monitor.evaluation.__main__ as cli

    monkeypatch.setattr(cli, "run_evaluation", lambda **kwargs: small_report)
    out = tmp_path / "report.json"
    assert cli_main(["--out", str(out), "--json"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["incident_metrics"]["detected"] == 2
    assert json.loads(capsys.readouterr().out)["seed"] == 3
