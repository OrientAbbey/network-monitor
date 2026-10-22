"""
Rapport de disponibilité : combien de temps chaque sortie a été en incident sur une période,
calculé à partir des incidents confirmés et de leurs dates **rétro-datées** (`started_at` →
`ended_at`), pas des dates de confirmation et de fermeture.

Définitions (volontairement simples et explicites, car un rapport se discute) :
- *indisponibilité* : temps couvert par au moins un incident dont la sévérité atteint
  `downtime_severity` (critique par défaut). Les incidents qui se chevauchent sur une même sortie
  ne sont comptés qu'une fois (union des intervalles) ;
- *dégradation* : temps couvert par des incidents confirmés moins sévères, hors indisponibilité ;
- *maintenance planifiée* : le temps couvert par une fenêtre de maintenance de la sortie est retiré de
  l'indisponibilité **et** du temps observé (convention des rapports SLA) ; `maintenance_seconds` le dit ;
- *faux positifs* : un incident jugé « faux positif » par un opérateur n'est pas compté (`false_positives_excluded`
  dit combien) sauf demande contraire ;
- *période observée* : un rapport sur 30 jours d'un système qui ne surveille que depuis une heure
  ne dit rien du reste du mois. La période rapportée commence à la **première mesure conservée**
  (`monitored_from`), pas au début demandé : la disponibilité se calcule sur ce qui a réellement
  été observé, et `monitored_seconds` dit combien cela représente ;
- *couverture* : part des mesures attendues effectivement reçues sur la période observée. Un trou
  de collecte n'est ni de la disponibilité ni de l'indisponibilité : la couverture est donnée à côté
  pour que le lecteur juge de la fiabilité ;
- *MTTD* : délai moyen entre le début réel et la confirmation ; *MTTR* : durée moyenne d'un incident.
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple

from network_monitor.app.models import (
    Incident,
    IncidentVerdict,
    MaintenanceWindow,
    Severity,
)

_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}

Interval = tuple[datetime, datetime]


@dataclass
class SampleStats:
    """Mesures conservées d'une sortie sur la période : leur nombre et la date de la première."""

    count: int
    first: datetime | None


@dataclass
class OutputAvailability:
    output_id: str
    period_seconds: float
    monitored_from: datetime | None
    monitored_seconds: float
    maintenance_seconds: float
    false_positives_excluded: int
    incidents: int
    downtime_seconds: float
    degraded_seconds: float
    availability_percent: float | None
    coverage_percent: float | None
    mttd_seconds: float | None
    mttr_seconds: float | None
    longest_incident_seconds: float | None


@dataclass
class AvailabilityReport:
    start: datetime
    end: datetime
    downtime_severity: str
    outputs: list[OutputAvailability] = field(default_factory=list)

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        columns = [f for f in OutputAvailability.__dataclass_fields__]
        writer.writerow(columns)
        for o in self.outputs:
            row = asdict(o)
            writer.writerow(["" if row[c] is None else (row[c].isoformat() if isinstance(row[c], datetime) else row[c]) for c in columns])
        return buf.getvalue()


def _union(intervals: list[Interval]) -> list[Interval]:
    """Fusionne les intervalles qui se chevauchent ou se touchent."""
    merged: list[Interval] = []
    for begin, finish in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return merged


def _total_seconds(intervals: list[Interval]) -> float:
    return sum((finish - begin).total_seconds() for begin, finish in intervals)


def _subtract(intervals: list[Interval], removed: list[Interval]) -> list[Interval]:
    """`intervals` privé de `removed` (les deux listes sont des unions triées)."""
    remaining: list[Interval] = []
    for begin, finish in intervals:
        cursor = begin
        for removed_begin, removed_finish in removed:
            if removed_finish <= cursor or removed_begin >= finish:
                continue
            if removed_begin > cursor:
                remaining.append((cursor, removed_begin))
            cursor = max(cursor, removed_finish)
        if cursor < finish:
            remaining.append((cursor, finish))
    return remaining


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _round_or_none(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


class _ReportPeriod(NamedTuple):
    """La fenêtre demandée, l'instant courant qui borne les incidents encore ouverts, et l'intervalle entre deux mesures."""

    start: datetime
    end: datetime
    now: datetime
    interval: timedelta

    @property
    def horizon(self) -> datetime:
        """Fin de ce qui a pu être observé : une période en cours n'a pas d'avenir à rapporter."""
        return min(self.end, self.now)


class _IncidentSpan(NamedTuple):
    incident: Incident
    start: datetime
    end: datetime


def _confirmed_incident_spans(
    incidents: list[Incident], output_id: str, observed: Interval, now: datetime, include_false_positives: bool
) -> tuple[list[_IncidentSpan], int]:
    """Les incidents confirmés de la sortie, rognés à la période observée, et le nombre de faux positifs écartés."""
    observed_from, horizon = observed
    spans: list[_IncidentSpan] = []
    excluded_false_positives = 0
    for incident in incidents:
        if incident.output_id != output_id or incident.opened_at is None:
            continue  # jamais confirmé : pas un incident
        if incident.verdict == IncidentVerdict.FALSE_POSITIVE and not include_false_positives:
            excluded_false_positives += 1
            continue
        began = incident.started_at or incident.opened_at
        ended = incident.ended_at or incident.closed_at or (now if incident.is_active else incident.last_seen_at)
        span_start, span_end = max(began, observed_from), min(ended, horizon)
        if span_start < span_end:
            spans.append(_IncidentSpan(incident, span_start, span_end))
    return spans, excluded_false_positives


def _maintenance_intervals(windows: list[MaintenanceWindow], output_id: str, observed: Interval) -> list[Interval]:
    """Les fenêtres de maintenance actives (non annulées) de la sortie ou de toutes, rognées à la période observée."""
    observed_from, horizon = observed
    clipped = [
        (max(window.start, observed_from), min(window.end, horizon))
        for window in windows
        if window.cancelled_at is None and window.output_id in (None, output_id)
    ]
    return _union([(begin, finish) for begin, finish in clipped if begin < finish])


def _coverage_percent(stats: SampleStats, observed_seconds: float, interval: timedelta) -> float:
    """Part des mesures attendues effectivement reçues (une mesure à chaque borne de la période observée)."""
    if not stats.first:
        return 0.0
    expected_samples = observed_seconds / interval.total_seconds() + 1
    return min(100.0, 100.0 * stats.count / expected_samples)


def _output_availability(
    output_id: str,
    stats: SampleStats,
    period: _ReportPeriod,
    incidents: list[Incident],
    maintenance: list[MaintenanceWindow],
    downtime_severity: Severity,
    include_false_positives: bool,
) -> OutputAvailability:
    # Période réellement observée : de la première mesure conservée (au plus tôt `start`) à `horizon`.
    observed_from = max(period.start, stats.first) if stats.first else period.horizon
    observed = (observed_from, period.horizon)
    spans, excluded_false_positives = _confirmed_incident_spans(incidents, output_id, observed, period.now, include_false_positives)

    maintenance_intervals = _maintenance_intervals(maintenance, output_id, observed)
    downtime_intervals = _subtract(
        _union([(span.start, span.end) for span in spans if _SEVERITY_RANK[span.incident.severity] >= _SEVERITY_RANK[downtime_severity]]),
        maintenance_intervals,
    )
    degraded_intervals = _subtract(_subtract(_union([(span.start, span.end) for span in spans]), downtime_intervals), maintenance_intervals)

    observed_seconds = max((period.horizon - observed_from).total_seconds(), 0.0)
    maintenance_seconds = _total_seconds(maintenance_intervals)
    due_seconds = observed_seconds - maintenance_seconds  # la maintenance planifiée n'est pas du temps « dû »
    downtime_seconds = _total_seconds(downtime_intervals)
    availability_percent = max(0.0, 100.0 * (1 - downtime_seconds / due_seconds)) if due_seconds > 0 else None

    incident_durations = [(span.end - span.start).total_seconds() for span in spans]
    detection_delays = [
        (span.incident.opened_at - max(span.incident.started_at or span.incident.opened_at, period.start)).total_seconds()
        for span in spans
        if span.incident.opened_at
    ]
    return OutputAvailability(
        output_id=output_id,
        period_seconds=(period.end - period.start).total_seconds(),
        monitored_from=observed_from if stats.first else None,
        monitored_seconds=round(observed_seconds, 1),
        maintenance_seconds=round(maintenance_seconds, 1),
        false_positives_excluded=excluded_false_positives,
        incidents=len(spans),
        downtime_seconds=round(downtime_seconds, 1),
        degraded_seconds=round(_total_seconds(degraded_intervals), 1),
        availability_percent=_round_or_none(availability_percent, 4),
        coverage_percent=round(_coverage_percent(stats, observed_seconds, period.interval), 2),
        mttd_seconds=_round_or_none(_mean(detection_delays), 1),
        mttr_seconds=_round_or_none(_mean(incident_durations), 1),
        longest_incident_seconds=round(max(incident_durations), 1) if incident_durations else None,
    )


def build_report(
    incidents: list[Incident],
    outputs: list[str],
    start: datetime,
    end: datetime,
    now: datetime,
    interval: timedelta,
    samples: dict[str, SampleStats],
    downtime_severity: Severity = Severity.CRITICAL,
    maintenance: list[MaintenanceWindow] | None = None,
    include_false_positives: bool = False,
) -> AvailabilityReport:
    """Rapport sur [start, end[. `now` borne les incidents encore ouverts ; `samples` donne, par
    sortie, les mesures conservées sur la période (les plus anciennes sont purgées : la période
    observée ne peut pas remonter plus loin que la première mesure conservée)."""
    if end <= start:
        raise ValueError("la fin de période doit suivre son début")
    period = _ReportPeriod(start, end, now, interval)
    report = AvailabilityReport(start, end, downtime_severity.value)
    for output_id in outputs:
        stats = samples.get(output_id, SampleStats(0, None))
        report.outputs.append(
            _output_availability(output_id, stats, period, incidents, maintenance or [], downtime_severity, include_false_positives)
        )
    return report
