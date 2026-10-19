"""
Évaluation de la détection contre la vérité terrain du Mock (§33).

Rapproche les incidents détectés (confirmés : `opened_at` renseigné) des
scénarios injectés par recouvrement temporel sur la même sortie, puis calcule
précision, rappel, F1, faux positifs et délais. À utiliser sur une période
distincte de la calibration.

    python -m network_monitor.analysis.evaluation --days 7 --seeds 1 2 3
    python -m network_monitor.analysis.evaluation --sweep      # balayage des seuils d'incident
    python -m network_monitor.analysis.evaluation --from-db sqlite:///network_monitor.db --days 30 --sweep

`--from-db` fait la même chose avec la **vérité terrain d'exploitation** : les anomalies enregistrées sont rejouées
avec d'autres seuils et comparées aux incidents que des opérateurs ont jugés (vrai ou faux positif).

Les anomalies d'une exécution sont enregistrées une fois (`capture`), puis
rejouées à travers le moteur d'incidents avec différents réglages (`replay`) :
seuls `open_score`, `close_score`, `warning_score` et les durées changent, pas
le score des anomalies, donc le rejeu est exact et ne coûte presque rien.
"""

from __future__ import annotations

import argparse
import itertools
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean

from network_monitor.app.config import AppConfig, HistoryConfig, IncidentConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import (
    AnomalyResult,
    Incident,
    IncidentType,
    IncidentVerdict,
    Severity,
)
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.incidents.manager import IncidentManager
from network_monitor.storage.repository import Repository, SqlAlchemyRepository

START = datetime(2026, 9, 1, tzinfo=timezone.utc)

_ANY_DROP = {IncidentType.TRAFFIC_DROP, IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS}
EXPECTED_TYPES: dict[ScenarioType, set[IncidentType]] = {
    ScenarioType.TRAFFIC_DROP: {IncidentType.TRAFFIC_DROP},
    ScenarioType.TRAFFIC_SPIKE: {IncidentType.TRAFFIC_SPIKE},
    ScenarioType.HIGH_LOSS: {IncidentType.HIGH_LOSS},
    ScenarioType.TRAFFIC_DROP_HIGH_LOSS: {IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS},
    ScenarioType.LONG_INCIDENT: {IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS},
    ScenarioType.PROGRESSIVE_DEGRADATION: {IncidentType.PROGRESSIVE_DEGRADATION},
    ScenarioType.INTERMITTENT_INCIDENT: {IncidentType.INTERMITTENT_DEGRADATION},
    # Trafic en baisse progressive + pertes par à-coups : plusieurs étiquettes sont défendables.
    ScenarioType.COMBINED: _ANY_DROP | {IncidentType.PROGRESSIVE_DEGRADATION, IncidentType.INTERMITTENT_DEGRADATION},
}
"""Type d'incident accepté pour chaque scénario (SHORT_INCIDENT et PROGRESSIVE_RECOVERY
n'ont pas d'étiquette attendue : le premier est plus court que `minimum_duration`)."""

_STANDARD = [  # (type, durée en minutes)
    (ScenarioType.TRAFFIC_DROP, 40), (ScenarioType.PROGRESSIVE_DEGRADATION, 90),
    (ScenarioType.INTERMITTENT_INCIDENT, 90), (ScenarioType.TRAFFIC_SPIKE, 40),
    (ScenarioType.HIGH_LOSS, 40), (ScenarioType.TRAFFIC_DROP_HIGH_LOSS, 40),
    (ScenarioType.LONG_INCIDENT, 300), (ScenarioType.COMBINED, 90),
]


def standard_scenarios(
    seed: int, days: float = 7, drop: float = 0.6, loss: float = 8.0, period_min: int = 15, duty: float = 0.5
) -> list[AnomalyScenario]:
    """
    Jeu de scénarios de référence : 4 par sortie, espacés d'au moins 6 h, dont
    les types tournent avec `seed` pour que chaque type apparaisse sur
    plusieurs graines. Les positions sont déterministes (aucun aléa caché).
    `drop`, `loss`, `period_min` et `duty` déclinent le jeu en variantes (amplitude,
    période et rapport cyclique) pour vérifier qu'un réglage ne colle pas à un seul jeu.
    """
    scenarios = []
    outputs = ["OUT_001", "OUT_002", "OUT_003"]
    slots = [0.4, 1.9, 3.6, 5.2]  # jours après le début
    for o, output in enumerate(outputs):
        for k, day in enumerate(slots):
            if day + 0.3 > days:
                continue
            kind, minutes = _STANDARD[(seed * 5 + o * 3 + k) % len(_STANDARD)]
            start = START + timedelta(days=day, hours=(o * 2 + seed) % 5)
            scenarios.append(AnomalyScenario(
                scenario_id=f"{output}-{k}", output_id=output, start_time=start,
                end_time=start + timedelta(minutes=minutes), type=kind, severity=Severity.CRITICAL,
                traffic_drop_fraction=drop, loss_offset_points=loss,
                intermittent_period=timedelta(minutes=period_min), intermittent_duty_cycle=duty,
            ))
    return scenarios


def evaluate(scenarios: list[AnomalyScenario], incidents: list[Incident], observed_days: float) -> dict:
    """Métriques §33 pour un jeu de scénarios réels et d'incidents détectés."""
    # (incident, ouverture) : `opened_at` est garanti non nul pour les incidents confirmés.
    confirmed = [(i, i.opened_at) for i in incidents if i.opened_at is not None]
    matched: set[str] = set()
    delays: list[float] = []
    onset_errors: list[float] = []
    duration_errors: list[float] = []
    reported_duration_errors: list[float] = []
    detected = 0
    typed = 0
    typed_ok = 0
    confusion: list[tuple[str, str]] = []

    for sc in scenarios:
        hits = [
            (i, opened) for i, opened in confirmed
            if i.output_id == sc.output_id and opened <= sc.end_time and (i.closed_at or i.last_seen_at) >= sc.start_time
        ]
        if not hits:
            continue
        detected += 1
        matched.update(i.id for i, _ in hits)
        first, first_open = min(hits, key=lambda h: h[1])
        delays.append(max(0.0, (first_open - sc.start_time).total_seconds() / 60))
        real = sc.end_time - sc.start_time
        # Avant rétro-datage : de l'ouverture à la fermeture. Après : du début à la fin rétro-datés.
        closed_end = max((i.closed_at or i.last_seen_at) for i, _ in hits)
        duration_errors.append(abs((closed_end - first_open) - real).total_seconds() / 60)
        start = first.started_at or first_open
        end = max((i.ended_at or i.closed_at or i.last_seen_at) for i, _ in hits)
        onset_errors.append(abs(start - sc.start_time).total_seconds() / 60)
        reported_duration_errors.append(abs((end - start) - real).total_seconds() / 60)
        expected = EXPECTED_TYPES.get(sc.type)
        if expected is not None:
            typed += 1
            typed_ok += first.incident_type in expected
            if first.incident_type not in expected:
                confusion.append((sc.type.value, first.incident_type.value))

    false_positives = len(confirmed) - len(matched)
    true_positives = len(matched)
    precision = true_positives / (true_positives + false_positives) if confirmed else 1.0
    recall = detected / len(scenarios) if scenarios else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "scenarios": len(scenarios), "detected": detected, "confirmed_incidents": len(confirmed),
        "false_positives": false_positives,
        "false_positives_per_day": false_positives / observed_days if observed_days else 0.0,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "mean_detection_delay_min": round(mean(delays), 1) if delays else None,
        "mean_duration_error_min": round(mean(duration_errors), 1) if duration_errors else None,
        "mean_onset_error_min": round(mean(onset_errors), 1) if onset_errors else None,
        "mean_reported_duration_error_min": round(mean(reported_duration_errors), 1) if reported_duration_errors else None,
        "type_accuracy": round(typed_ok / typed, 3) if typed else None,
        "typed": typed, "type_errors": confusion,
    }


@dataclass
class Capture:
    """Anomalies d'une exécution, dans l'ordre où le moteur d'incidents les a reçues."""

    records: list[tuple[AnomalyResult, float | None, float | None, float | None]]
    scenarios: list[AnomalyScenario]
    days: float


def capture(seed: int, days: float, scenarios: list[AnomalyScenario] | None = None) -> Capture:
    """Calibre sur l'historique (40 j), puis observe `days` jours *après* la calibration."""
    config = AppConfig(history=HistoryConfig(minimum=timedelta(days=30), preferred=timedelta(days=40)))
    source = MockDataSource(seed=seed)
    for sc in scenarios or []:
        source.add_scenario(sc)
    records: list[tuple[AnomalyResult, float | None, float | None, float | None]] = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = SqlAlchemyRepository(f"sqlite:///{tmp}/eval.db")
        try:
            loop = MonitoringLoop(source, repo, config)
            original = loop.incident_manager.process

            def recording(result, expected_traffic_mbps=None, expected_loss_percent=None,
                          observed_traffic_mbps=None, observed_loss_percent=None):
                records.append((result.model_copy(deep=True), expected_traffic_mbps, observed_traffic_mbps, observed_loss_percent))
                return original(result, expected_traffic_mbps, expected_loss_percent,
                                observed_traffic_mbps, observed_loss_percent)

            loop.incident_manager.process = recording  # type: ignore[method-assign]
            loop.calibrate(START)
            loop.run_forever(START, max_ticks=int(days * 24 * 60 / (config.monitoring.interval.total_seconds() / 60)))
        finally:
            repo.dispose()
    return Capture(records, source.ground_truth(), days)


def replay(cap: Capture, incident: dict | None = None) -> dict:
    """Rejoue les anomalies enregistrées avec d'autres réglages d'incident, puis évalue."""
    cfg = IncidentConfig(**(incident or {}))
    manager = IncidentManager(cfg)
    touched: dict[str, Incident] = {}
    for result, expected, observed, observed_loss in cap.records:
        _, inc = manager.process(result, expected_traffic_mbps=expected,
                                 observed_traffic_mbps=observed, observed_loss_percent=observed_loss)
        if inc is not None:
            touched[inc.id] = inc
    return evaluate(cap.scenarios, list(touched.values()), cap.days)


def run_evaluation(
    seed: int, days: float, scenarios: list[AnomalyScenario] | None = None, incident: dict | None = None
) -> dict:
    """Une exécution complète (capture + rejeu). `incident` surcharge des champs de
    `IncidentConfig` (ex. `{"minimum_duration": "15m"}`)."""
    return replay(capture(seed, days, scenarios), incident)


GRID: dict[str, list] = {
    "open_score": [0.65, 0.75, 0.85],
    "close_score": [0.3, 0.4, 0.5],
    "minimum_duration": ["10m", "15m", "20m"],
    "recovery_duration": ["5m", "10m", "15m"],
    "min_suspect_steps": [2, 3, 4],
}


def _configs(grid: dict[str, list] | None) -> Iterator[dict]:
    """Combinaisons de la grille dont les seuils restent ordonnés (close < warning < open)."""
    axes: dict[str, list] = grid if grid is not None else GRID
    for values in itertools.product(*axes.values()):
        cfg = dict(zip(axes, values, strict=True))
        if cfg.get("close_score", 0.4) < cfg.get("warning_score", 0.5) < cfg.get("open_score", 0.75):
            yield cfg


def sweep(captures: list[Capture], grid: dict[str, list] | None = None) -> list[dict]:
    """Évalue chaque combinaison de la grille sur toutes les captures ; agrège par somme."""
    rows = []
    for cfg in _configs(grid):
        runs = [replay(c, cfg) for c in captures]
        typed = sum(r["typed"] for r in runs)
        rows.append({
            "config": cfg,
            "detected": sum(r["detected"] for r in runs), "scenarios": sum(r["scenarios"] for r in runs),
            "false_positives": sum(r["false_positives"] for r in runs),
            "delay": round(mean(r["mean_detection_delay_min"] for r in runs if r["mean_detection_delay_min"] is not None), 1),
            "duration_error": round(mean(r["mean_duration_error_min"] for r in runs if r["mean_duration_error_min"] is not None), 1),
            "type_accuracy": round(sum(r["type_accuracy"] * r["typed"] for r in runs if r["typed"]) / typed, 3) if typed else None,
        })
    return rows


@dataclass
class StoredCapture:
    """Anomalies enregistrées en exploitation et incidents que des opérateurs ont jugés."""

    records: list[tuple[AnomalyResult, float | None, float | None, float | None]]
    judged: list[Incident]
    unjudged_original: int


def capture_from_storage(storage: Repository, start: datetime, end: datetime) -> StoredCapture:
    """Relit la base : les anomalies des sorties qui ont eu un incident confirmé dans [start, end), avec la
    prévision (attendu) et la mesure brute (observé) quand elles existent encore. Le rejeu démarre avant le
    premier signal faible de chaque incident jugé, pour qu'aucun ne soit coupé par le début de la fenêtre."""
    incidents = storage.list_incidents_between(start, end)
    judged = [i for i in incidents if i.verdict is not None]
    begin = min([start, *(i.suspected_at or i.started_at or i.opened_at or start for i in judged)])
    records: list[tuple[AnomalyResult, float | None, float | None, float | None]] = []
    for output_id in sorted({i.output_id for i in incidents}):
        expected = {f.timestamp: f.predicted_traffic_mbps for f in storage.get_forecasts(output_id, begin, end)}
        measured = {m.timestamp: m for m in storage.get_history(output_id, begin, end)}
        for anomaly in storage.get_anomalies(output_id, begin, end):
            sample = measured.get(anomaly.timestamp)
            if anomaly.data_gap:  # comme la boucle : un trou n'a ni attendu ni observé
                records.append((anomaly, None, None, None))
            else:
                records.append((anomaly, expected.get(anomaly.timestamp), sample.traffic_mbps if sample else None,
                                sample.loss_percent if sample else None))
    return StoredCapture(records, judged, len(incidents) - len(judged))


def replay_judged(cap: StoredCapture, incident: dict | None = None) -> dict:
    """Rejoue avec d'autres réglages, puis compte, parmi les incidents **jugés**, ceux qui seraient encore
    détectés (vrais positifs) ou encore ouverts (faux positifs). Un incident rejoué qui ne recoupe aucun incident
    jugé est ignoré (`unjudged_ignored`) : on ne sait pas s'il serait bon."""
    manager = IncidentManager(IncidentConfig(**(incident or {})))
    touched: dict[str, Incident] = {}
    for result, expected, observed, observed_loss in cap.records:
        _, inc = manager.process(result, expected_traffic_mbps=expected,
                                 observed_traffic_mbps=observed, observed_loss_percent=observed_loss)
        if inc is not None:
            touched[inc.id] = inc
    replayed = [i for i in touched.values() if i.opened_at is not None]

    def overlaps(r: Incident, j: Incident) -> bool:
        end = j.ended_at or j.closed_at or j.last_seen_at
        return bool(r.output_id == j.output_id and r.opened_at and r.opened_at <= end
                    and (r.closed_at or r.last_seen_at) >= (j.started_at or j.opened_at or j.last_seen_at))

    def still(verdict: IncidentVerdict) -> tuple[int, int]:
        group = [j for j in cap.judged if j.verdict == verdict]
        return len(group), sum(any(overlaps(r, j) for r in replayed) for j in group)

    tp, tp_kept = still(IncidentVerdict.TRUE_POSITIVE)
    fp, fp_kept = still(IncidentVerdict.FALSE_POSITIVE)
    return {
        "true_positives": tp, "true_positives_still_detected": tp_kept,
        "false_positives": fp, "false_positives_still_opened": fp_kept,
        "recall_on_judged": round(tp_kept / tp, 3) if tp else None,
        "unjudged_ignored": sum(not any(overlaps(r, j) for j in cap.judged) for r in replayed),
        "unjudged_original": cap.unjudged_original,
    }


def replay_from_storage(storage: Repository, start: datetime, end: datetime, overrides: dict | None = None) -> dict:
    """`capture_from_storage` puis `replay_judged`, pour un seul jeu de réglages."""
    return replay_judged(capture_from_storage(storage, start, end), overrides)


def sweep_judged(cap: StoredCapture, grid: dict[str, list] | None = None) -> list[dict]:
    """Balaye la grille de seuils sur les verdicts : le meilleur réglage garde les vrais incidents et
    n'ouvre plus les faux."""
    return [{"config": cfg, **replay_judged(cap, cfg)} for cfg in _configs(grid)]


if __name__ == "__main__":  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--sweep", action="store_true", help="balaye la grille de seuils sur les graines données")
    parser.add_argument("--from-db", metavar="URL", help="rejoue les anomalies d'une base d'exploitation contre les verdicts des opérateurs")
    parser.add_argument("--end", help="fin de la fenêtre (ISO 8601 avec fuseau) ; par défaut maintenant, à préciser pour une base à horloge simulée")
    args = parser.parse_args()
    if args.from_db:
        stop = datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)
        db = SqlAlchemyRepository(args.from_db)
        try:
            stored = capture_from_storage(db, stop - timedelta(days=args.days), stop)
        finally:
            db.dispose()
        print(f"{len(stored.judged)} incident(s) jugé(s), {stored.unjudged_original} sans verdict (ignorés)")
        if args.sweep:
            for row in sorted(sweep_judged(stored), key=lambda r: (r["false_positives_still_opened"], -r["true_positives_still_detected"])):
                print(row)
        else:
            print(replay_judged(stored))
        raise SystemExit(0)
    caps = [capture(seed, args.days, standard_scenarios(seed, args.days)) for seed in args.seeds]
    if args.sweep:
        for row in sorted(sweep(caps), key=lambda r: (r["false_positives"], -r["detected"], r["delay"])):
            print(row)
    else:
        for seed, cap in zip(args.seeds, caps, strict=True):
            print(f"seed={seed}", replay(cap))
