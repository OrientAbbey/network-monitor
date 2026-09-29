"""
Banc d'évaluation (§33) : exécute le pipeline complet (calibration → boucle de
supervision → incidents) sur le Mock avec une suite de scénarios connus, puis
compare détections et prévisions à la vérité terrain.

Protocole : la calibration utilise l'historique qui PRÉCÈDE `eval_start` ; les
scénarios sont tous injectés APRÈS `eval_start` (refusé sinon). L'évaluation
porte donc toujours sur une période distincte de la calibration, comme
l'exige le §33.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from network_monitor.app.config import AppConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import Incident, Severity
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.evaluation.metrics import (
    MatchResult,
    false_positive_rate,
    forecast_metrics,
    match_incidents,
    summarize_delays,
)
from network_monitor.storage.repository import SqlAlchemyRepository

DEFAULT_EVAL_START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

# Durée de chaque type de scénario dans la suite par défaut.
_DURATIONS = {
    ScenarioType.SHORT_INCIDENT: timedelta(minutes=15),
    ScenarioType.LONG_INCIDENT: timedelta(minutes=90),
    ScenarioType.PROGRESSIVE_DEGRADATION: timedelta(minutes=60),
    ScenarioType.INTERMITTENT_INCIDENT: timedelta(minutes=60),
}
_DEFAULT_DURATION = timedelta(minutes=40)


def evaluation_config() -> AppConfig:
    """
    Configuration par défaut de l'évaluation : 40 jours d'historique (comme
    l'API de démonstration), rétention illimitée (rien ne doit être purgé
    pendant la mesure). La recalibration périodique reste active.
    """
    return AppConfig.model_validate(
        {"history": {"minimum": "30d", "preferred": "40d"}, "storage": {"retention": None}}
    )


def default_scenarios(
    eval_start: datetime = DEFAULT_EVAL_START,
    output_ids: list[str] | None = None,
    spacing: timedelta = timedelta(hours=4),
) -> list[AnomalyScenario]:
    """
    Une occurrence de chacun des dix types de scénarios (§7), espacées de
    `spacing` et réparties tour à tour sur les sorties, à partir de
    `eval_start + 3 h`.
    """
    outputs = output_ids or ["OUT_001", "OUT_002", "OUT_003"]
    scenarios = []
    for i, sc_type in enumerate(ScenarioType):
        start = eval_start + timedelta(hours=3) + i * spacing
        scenarios.append(
            AnomalyScenario(
                scenario_id=f"EVAL_{i:02d}_{sc_type.value}",
                output_id=outputs[i % len(outputs)],
                start_time=start,
                end_time=start + _DURATIONS.get(sc_type, _DEFAULT_DURATION),
                type=sc_type,
                severity=Severity.CRITICAL,
            )
        )
    return scenarios


@dataclass
class EvaluationReport:
    eval_start: datetime
    eval_end: datetime
    calibration_end: datetime
    seed: int
    incident_metrics: dict[str, Any]
    forecast_metrics: dict[str, Any]
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    false_positive_incidents: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": {
                "calibration_end": self.calibration_end.isoformat(),
                "eval_start": self.eval_start.isoformat(),
                "eval_end": self.eval_end.isoformat(),
            },
            "seed": self.seed,
            "incident_metrics": self.incident_metrics,
            "forecast_metrics": self.forecast_metrics,
            "scenarios": self.scenarios,
            "false_positive_incidents": self.false_positive_incidents,
        }

    def format_text(self) -> str:
        m = self.incident_metrics

        def pct(x: float | None) -> str:
            return "n/a" if x is None else f"{100 * x:.1f} %"

        def mins(x: float | None) -> str:
            return "n/a" if x is None else f"{x / 60:.1f} min"

        lines = [
            f"Évaluation du {self.eval_start:%Y-%m-%d %H:%M} au {self.eval_end:%Y-%m-%d %H:%M} "
            f"(calibration jusqu'au {self.calibration_end:%Y-%m-%d %H:%M}, seed {self.seed})",
            "",
            "Incidents",
            f"  scénarios : {m['scenarios']}   détectés : {m['detected']}   incidents confirmés : {m['incidents']}"
            f"   faux positifs : {m['false_positive_incidents']}",
            f"  précision : {pct(m['precision'])}   rappel : {pct(m['recall'])}   F1 : {pct(m['f1'])}"
            f"   taux de faux positifs (pas normaux) : {pct(m['false_positive_rate'])}",
            f"  délai de détection  moy/méd/max : {mins(m['detection_delay']['mean_s'])} / "
            f"{mins(m['detection_delay']['median_s'])} / {mins(m['detection_delay']['max_s'])}",
            f"  délai de fermeture  moy/méd/max : {mins(m['closure_delay']['mean_s'])} / "
            f"{mins(m['closure_delay']['median_s'])} / {mins(m['closure_delay']['max_s'])}",
            f"  erreur de durée     moy/méd/max : {mins(m['duration_error']['mean_s'])} / "
            f"{mins(m['duration_error']['median_s'])} / {mins(m['duration_error']['max_s'])}",
            "",
            "Prévision (hors périodes d'anomalie)",
        ]
        f = self.forecast_metrics["overall"]
        if f["n"]:
            lines.append(
                f"  n={f['n']}  MAE={f['mae']:.1f} Mbps  RMSE={f['rmse']:.1f} Mbps  sMAPE={f['smape_percent']:.1f} %  "
                f"couverture intervalle={pct(f['picp'])} (nominale {pct(f['nominal_coverage'])})"
            )
        else:
            lines.append("  aucune donnée")
        lines += ["", "Détail par scénario"]
        for s in self.scenarios:
            if s["detected"]:
                lines.append(
                    f"  {s['scenario_id']:<38} {s['output_id']}  détecté  détection +{mins(s['detection_delay_s'])}"
                    f"  fermeture +{mins(s['closure_delay_s'])}  fragments={s['fragments']}"
                )
            else:
                lines.append(f"  {s['scenario_id']:<38} {s['output_id']}  MANQUÉ")
        return "\n".join(lines)


def _scenario_row(r) -> dict[str, Any]:
    def secs(d: timedelta | None) -> float | None:
        return None if d is None else d.total_seconds()

    return {
        "scenario_id": r.scenario_id,
        "type": r.scenario_type,
        "output_id": r.output_id,
        "true_start": r.true_start.isoformat(),
        "true_end": r.true_end.isoformat(),
        "predicted_start": r.predicted_start.isoformat() if r.predicted_start else None,
        "predicted_end": r.predicted_end.isoformat() if r.predicted_end else None,
        "detected": r.detected,
        "fragments": r.fragments,
        "detection_delay_s": secs(r.detection_delay),
        "closure_delay_s": secs(r.closure_delay),
        "duration_error_s": secs(r.duration_error),
    }


def build_incident_metrics(
    match: MatchResult, fpr: float | None
) -> dict[str, Any]:
    detected = [s for s in match.scenarios if s.detected]
    by_type: dict[str, dict[str, Any]] = {}
    for s in match.scenarios:
        entry = by_type.setdefault(s.scenario_type, {"scenarios": 0, "detected": 0})
        entry["scenarios"] += 1
        entry["detected"] += int(s.detected)
    return {
        "scenarios": len(match.scenarios),
        "detected": match.detected_count,
        "incidents": match.total_incidents,
        "true_positive_incidents": match.true_positive_incidents,
        "false_positive_incidents": len(match.false_positive_incidents),
        "precision": match.precision,
        "recall": match.recall,
        "f1": match.f1,
        "false_positive_rate": fpr,
        "detection_delay": summarize_delays([s.detection_delay for s in detected if s.detection_delay is not None]),
        "closure_delay": summarize_delays([s.closure_delay for s in detected if s.closure_delay is not None]),
        "duration_error": summarize_delays([s.duration_error for s in detected if s.duration_error is not None]),
        "by_scenario_type": by_type,
    }


def run_evaluation(
    *,
    seed: int = 42,
    eval_start: datetime = DEFAULT_EVAL_START,
    eval_days: float = 2.0,
    scenarios: list[AnomalyScenario] | None = None,
    config: AppConfig | None = None,
) -> EvaluationReport:
    """
    Exécute l'évaluation complète et renvoie le rapport.

    Lève `ValueError` si un scénario commence avant `eval_start` (il serait
    appris pendant la calibration) ou finit après la fin de la période.
    """
    config = config or evaluation_config()
    interval = config.monitoring.interval
    eval_end = eval_start + timedelta(days=eval_days)
    scenarios = scenarios if scenarios is not None else default_scenarios(eval_start)

    for sc in scenarios:
        if sc.start_time < eval_start:
            raise ValueError(
                f"Scénario {sc.scenario_id} commence avant eval_start : il serait inclus dans la période "
                "de calibration (§33 exige des périodes distinctes)."
            )
        if sc.end_time > eval_end:
            raise ValueError(f"Scénario {sc.scenario_id} se termine après la fin de la période d'évaluation.")

    source = MockDataSource(seed=seed)
    for sc in scenarios:
        source.add_scenario(sc)

    with tempfile.TemporaryDirectory() as tmp:
        storage = SqlAlchemyRepository(f"sqlite:///{tmp}/evaluation.db")
        try:
            loop = MonitoringLoop(source, storage, config)
            loop.calibrate(eval_start)
            n_ticks = int((eval_end - eval_start) / interval)
            loop.run_forever(eval_start, max_ticks=n_ticks)

            incidents: list[Incident] = storage.list_incidents(limit=100_000)
            outputs = source.list_outputs()
            forecasts, samples = [], []
            for output_id in outputs:
                forecasts += storage.get_forecasts(output_id, eval_start, eval_end)
                samples += storage.get_history(output_id, eval_start, eval_end)
        finally:
            storage.dispose()

    truth = source.ground_truth()
    match = match_incidents(truth, incidents)
    grace = config.incident.recovery_duration + interval
    fpr = false_positive_rate(truth, incidents, outputs, eval_start, eval_end, interval, grace)

    exclude = [(s.output_id, (s.start_time, s.end_time + grace)) for s in truth]
    per_output = {
        oid: forecast_metrics(
            [f for f in forecasts if f.output_id == oid], [s for s in samples if s.output_id == oid], exclude
        )
        for oid in outputs
    }

    return EvaluationReport(
        eval_start=eval_start,
        eval_end=eval_end,
        calibration_end=eval_start,
        seed=seed,
        incident_metrics=build_incident_metrics(match, fpr),
        forecast_metrics={"overall": forecast_metrics(forecasts, samples, exclude), "per_output": per_output},
        scenarios=[_scenario_row(r) for r in match.scenarios],
        false_positive_incidents=[
            {
                "incident_id": i.id,
                "output_id": i.output_id,
                "opened_at": i.opened_at.isoformat() if i.opened_at else None,
                "closed_at": i.closed_at.isoformat() if i.closed_at else None,
                "type": i.incident_type.value,
            }
            for i in match.false_positive_incidents
        ],
    )
