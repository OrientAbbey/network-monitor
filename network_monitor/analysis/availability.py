"""
Rapport de disponibilité (§33) : combien de temps chaque sortie a été en incident sur une période,
calculé à partir des incidents confirmés et de leurs dates **rétro-datées** (`started_at` →
`ended_at`), pas des dates de confirmation et de fermeture.

Définitions (volontairement simples et explicites, car un rapport se discute) :
- *indisponibilité* : temps couvert par au moins un incident dont la sévérité atteint
  `downtime_severity` (critique par défaut). Les incidents qui se chevauchent sur une même sortie
  ne sont comptés qu'une fois (union des intervalles) ;
- *dégradation* : temps couvert par des incidents confirmés moins sévères, hors indisponibilité ;
- *couverture* : part des mesures attendues effectivement reçues. Un trou de collecte n'est ni
  de la disponibilité ni de l'indisponibilité : la disponibilité est rapportée sur le temps
  couvert, et la couverture est donnée à côté pour que le lecteur juge de sa fiabilité ;
- *MTTD* : délai moyen entre le début réel et la confirmation ; *MTTR* : durée moyenne d'un incident.
"""

from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from network_monitor.app.models import Incident, Severity

_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


@dataclass
class OutputAvailability:
    output_id: str
    period_seconds: float
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
            writer.writerow(["" if row[c] is None else row[c] for c in columns])
        return buf.getvalue()


def _union(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    return sum((hi - lo).total_seconds() for lo, hi in intervals)


def _minus(intervals: list[tuple[datetime, datetime]], cut: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """`intervals` privé de `cut` (les deux listes sont des unions triées)."""
    out = []
    for lo, hi in intervals:
        cursor = lo
        for c_lo, c_hi in cut:
            if c_hi <= cursor or c_lo >= hi:
                continue
            if c_lo > cursor:
                out.append((cursor, c_lo))
            cursor = max(cursor, c_hi)
        if cursor < hi:
            out.append((cursor, hi))
    return out


def build_report(
    incidents: list[Incident],
    outputs: list[str],
    start: datetime,
    end: datetime,
    now: datetime,
    interval: timedelta,
    samples_received: dict[str, int],
    downtime_severity: Severity = Severity.CRITICAL,
    coverage_start: datetime | None = None,
) -> AvailabilityReport:
    """Rapport sur [start, end[. `now` borne les incidents encore ouverts ; `samples_received`
    donne, par sortie, le nombre de mesures reçues depuis `coverage_start` (début de la période
    si omis) : les mesures brutes les plus anciennes sont purgées, la couverture ne peut donc
    être jugée que sur la part encore conservée."""
    if end <= start:
        raise ValueError("la fin de période doit suivre son début")
    horizon = min(end, now)
    period = (end - start).total_seconds()
    report = AvailabilityReport(start, end, downtime_severity.value)

    for output_id in outputs:
        mine = []
        for i in incidents:
            if i.output_id != output_id or i.opened_at is None:
                continue  # jamais confirmé : pas un incident
            lo = i.started_at or i.opened_at
            hi = i.ended_at or i.closed_at or (now if i.is_active else i.last_seen_at)
            lo, hi = max(lo, start), min(hi, horizon)
            if lo < hi:
                mine.append((i, lo, hi))

        down = _union([(lo, hi) for i, lo, hi in mine if _RANK[i.severity] >= _RANK[downtime_severity]])
        degraded = _minus(_union([(lo, hi) for i, lo, hi in mine]), down)

        observed = max((horizon - start).total_seconds(), 0.0)  # période déjà écoulée
        covered_from = max(start, coverage_start) if coverage_start else start
        expected = max((horizon - covered_from).total_seconds(), 0.0) / interval.total_seconds()
        received = samples_received.get(output_id, 0)
        coverage = min(100.0, 100.0 * received / expected) if expected >= 1 else None
        down_s = _seconds(down)
        availability = max(0.0, 100.0 * (1 - down_s / observed)) if observed > 0 else None

        durations = [(hi - lo).total_seconds() for _i, lo, hi in mine]
        detect = [(i.opened_at - max(i.started_at or i.opened_at, start)).total_seconds() for i, _lo, _hi in mine if i.opened_at]
        report.outputs.append(
            OutputAvailability(
                output_id=output_id,
                period_seconds=period,
                incidents=len(mine),
                downtime_seconds=round(down_s, 1),
                degraded_seconds=round(_seconds(degraded), 1),
                availability_percent=None if availability is None else round(availability, 4),
                coverage_percent=None if coverage is None else round(coverage, 2),
                mttd_seconds=round(sum(detect) / len(detect), 1) if detect else None,
                mttr_seconds=round(sum(durations) / len(durations), 1) if durations else None,
                longest_incident_seconds=round(max(durations), 1) if durations else None,
            )
        )
    return report
