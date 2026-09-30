"""
Métriques d'évaluation (§33), fonctions pures.

Conventions :
- Seuls les incidents confirmés (`opened_at` renseigné) comptent : un signal faible jamais
  confirmé n'est pas un incident (§20).
- Un incident explique un scénario s'il concerne la même sortie et que leurs fenêtres se recoupent
  (incident : `opened_at` → `closed_at` ou `last_seen_at` ; scénario : `start_time` → `end_time`).
- Scénario détecté = expliqué par au moins un incident. Incident vrai positif = explique au moins
  un scénario, sinon faux positif. Plusieurs incidents sur un scénario (fragmentation) sont tous
  des vrais positifs, et comptés à part.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from network_monitor.app.models import ForecastResult, Incident, TrafficSample
from network_monitor.data.mock import AnomalyScenario

Window = tuple[datetime, datetime]


def _incident_window(incident: Incident) -> Window:
    assert incident.opened_at is not None
    return incident.opened_at, incident.closed_at or incident.last_seen_at


def _overlaps(a: Window, b: Window) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _confirmed(incidents: list[Incident]) -> list[Incident]:
    return [i for i in incidents if i.opened_at is not None]


@dataclass
class ScenarioResult:
    """Résultat pour un scénario : vérité terrain (true_*) et détection (predicted_*)."""

    scenario_id: str
    scenario_type: str
    output_id: str
    true_start: datetime
    true_end: datetime
    detected: bool
    predicted_start: datetime | None = None
    predicted_end: datetime | None = None
    fragments: int = 0
    """Nombre d'incidents confirmés qui recoupent ce scénario (>1 = fragmentation)."""
    detection_delay: timedelta | None = None
    closure_delay: timedelta | None = None
    duration_error: timedelta | None = None


@dataclass
class MatchResult:
    scenarios: list[ScenarioResult]
    total_incidents: int
    true_positive_incidents: int
    false_positive_incidents: list[Incident] = field(default_factory=list)

    @property
    def detected_count(self) -> int:
        return sum(1 for s in self.scenarios if s.detected)

    @property
    def recall(self) -> float | None:
        return self.detected_count / len(self.scenarios) if self.scenarios else None

    @property
    def precision(self) -> float | None:
        return self.true_positive_incidents / self.total_incidents if self.total_incidents else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)


def match_incidents(scenarios: list[AnomalyScenario], incidents: list[Incident]) -> MatchResult:
    """Apparie la vérité terrain aux incidents détectés (voir les conventions du module)."""
    confirmed = _confirmed(incidents)
    results: list[ScenarioResult] = []
    explained_ids: set[str] = set()

    for sc in sorted(scenarios, key=lambda s: s.start_time):
        sc_window = (sc.start_time, sc.end_time)
        overlapping = sorted(
            (i for i in confirmed if i.output_id == sc.output_id and _overlaps(_incident_window(i), sc_window)),
            key=lambda i: i.opened_at,  # type: ignore[arg-type,return-value]
        )
        explained_ids.update(i.id for i in overlapping)
        if not overlapping:
            results.append(
                ScenarioResult(sc.scenario_id, sc.type.value, sc.output_id, sc.start_time, sc.end_time, detected=False)
            )
            continue

        first, last = overlapping[0], overlapping[-1]
        start = first.opened_at
        end = last.closed_at
        assert start is not None
        pred_end = end or last.last_seen_at
        results.append(
            ScenarioResult(
                scenario_id=sc.scenario_id,
                scenario_type=sc.type.value,
                output_id=sc.output_id,
                true_start=sc.start_time,
                true_end=sc.end_time,
                detected=True,
                predicted_start=start,
                predicted_end=pred_end,
                fragments=len(overlapping),
                detection_delay=start - sc.start_time,
                closure_delay=(end - sc.end_time) if end is not None else None,
                duration_error=abs((pred_end - start) - (sc.end_time - sc.start_time)),
            )
        )

    false_positives = [i for i in confirmed if i.id not in explained_ids]
    return MatchResult(
        scenarios=results,
        total_incidents=len(confirmed),
        true_positive_incidents=len(confirmed) - len(false_positives),
        false_positive_incidents=false_positives,
    )


def false_positive_rate(
    scenarios: list[AnomalyScenario],
    incidents: list[Incident],
    output_ids: list[str],
    start: datetime,
    end: datetime,
    interval: timedelta,
    grace: timedelta = timedelta(0),
) -> float | None:
    """
    Part des pas de temps NORMAUX (hors scénario) couverts par un incident
    confirmé. `grace` prolonge la fin de chaque scénario : après la fin réelle
    de l'anomalie, l'incident reste légitimement ouvert le temps de la
    récupération (hystérésis, §21) — c'est mesuré par le délai de fermeture,
    pas compté comme faux positif. Renvoie None s'il n'y a aucun pas normal.
    """
    confirmed = _confirmed(incidents)
    normal = flagged = 0
    for output_id in output_ids:
        sc_windows = [(s.start_time, s.end_time + grace) for s in scenarios if s.output_id == output_id]
        inc_windows = [_incident_window(i) for i in confirmed if i.output_id == output_id]
        t = start
        while t < end:
            if not any(a <= t <= b for a, b in sc_windows):
                normal += 1
                if any(a <= t <= b for a, b in inc_windows):
                    flagged += 1
            t += interval
    return flagged / normal if normal else None


def summarize_delays(values: list[timedelta]) -> dict[str, float | int | None]:
    """Moyenne / médiane / max d'une série de délais, en secondes (None si vide)."""
    if not values:
        return {"count": 0, "mean_s": None, "median_s": None, "max_s": None}
    seconds = [v.total_seconds() for v in values]
    return {
        "count": len(seconds),
        "mean_s": statistics.fmean(seconds),
        "median_s": statistics.median(seconds),
        "max_s": max(seconds),
    }


def forecast_metrics(
    forecasts: list[ForecastResult],
    samples: list[TrafficSample],
    exclude: list[tuple[str, Window]] | None = None,
) -> dict[str, float | int | None]:
    """
    Qualité des prévisions : MAE, RMSE (en Mbps), sMAPE (en %) et couverture de
    l'intervalle prédictif (PICP), avec la couverture nominale moyenne pour la
    comparer. Les couples (sortie, horodatage) tombant dans une fenêtre
    d'`exclude` — typiquement les scénarios d'anomalie — sont écartés : une
    prévision est censée décrire le comportement NORMAL, l'écart avec une
    observation volontairement anormale n'est pas une erreur du modèle.
    """
    observed = {(s.output_id, s.timestamp): s.traffic_mbps for s in samples}
    excluded = exclude or []
    errors: list[float] = []
    smape_terms: list[float] = []
    inside = 0
    nominal: list[float] = []

    for f in forecasts:
        actual = observed.get((f.output_id, f.timestamp))
        if actual is None:
            continue
        if any(oid == f.output_id and a <= f.timestamp <= b for oid, (a, b) in excluded):
            continue
        errors.append(f.predicted_traffic_mbps - actual)
        denom = abs(f.predicted_traffic_mbps) + abs(actual)
        if denom > 0:
            smape_terms.append(2 * abs(f.predicted_traffic_mbps - actual) / denom)
        if f.lower_bound_mbps <= actual <= f.upper_bound_mbps:
            inside += 1
        nominal.append(f.confidence)

    n = len(errors)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "smape_percent": None, "picp": None, "nominal_coverage": None}
    return {
        "n": n,
        "mae": statistics.fmean(abs(e) for e in errors),
        "rmse": math.sqrt(statistics.fmean(e * e for e in errors)),
        "smape_percent": 100 * statistics.fmean(smape_terms) if smape_terms else None,
        "picp": inside / n,
        "nominal_coverage": statistics.fmean(nominal),
    }
