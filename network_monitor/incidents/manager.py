"""
Moteur d'incidents (§19-§27) : transforme des scores d'anomalie ponctuels
en incidents avec hystérésis (§21), sans jamais dépendre d'un algorithme de
détection particulier (§4).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from network_monitor.app.config import IncidentConfig
from network_monitor.app.models import (
    Alert,
    AlertEvent,
    AnomalyResult,
    Incident,
    IncidentEvidence,
    IncidentStatus,
    IncidentType,
    Severity,
)


@dataclass
class _OutputState:
    """État interne suivi par sortie (durées écoulées, incident en cours)."""

    suspect_since: datetime | None = None
    open_since: datetime | None = None
    below_close_since: datetime | None = None
    incident: Incident | None = None
    last_valid_timestamp: datetime | None = None


def _classify_type(traffic_score: float, loss_score: float, traffic_deviation_pct: float | None) -> IncidentType:
    """Détermine le type d'incident à partir des signaux (§23, §25)."""
    traffic_relevant = traffic_score > 0.3
    loss_relevant = loss_score > 0.3
    rising_traffic = (traffic_deviation_pct or 0.0) > 0

    if traffic_relevant and loss_relevant:
        return IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS
    if traffic_relevant and rising_traffic:
        return IncidentType.TRAFFIC_SPIKE
    if traffic_relevant:
        return IncidentType.TRAFFIC_DROP
    return IncidentType.HIGH_LOSS


class IncidentManager:
    """
    Gestionnaire du cycle de vie des incidents (§19-§27), à états
    SUSPECT / OPEN / STALE / CLOSED (§19, révisé). Indépendant des
    algorithmes de detection : ne consomme que des `AnomalyResult`.
    """

    def __init__(self, config: IncidentConfig, critical_score: float = 0.80, alerts_enabled: bool = True) -> None:
        self._config = config
        self._critical_score = critical_score
        # `alerts.enabled` (§35) : coupe la génération des alertes ; le cycle de
        # vie des incidents, lui, n'est jamais affecté.
        self._alerts_enabled = alerts_enabled
        self._states: dict[str, _OutputState] = {}

    def update_config(self, config: IncidentConfig, critical_score: float | None = None) -> None:
        """
        Remplace la configuration à chaud (§35), typiquement depuis l'API
        (PUT /config). N'affecte que les décisions futures : les incidents
        déjà ouverts continuent d'évoluer normalement, sans être recalculés
        rétroactivement avec les nouveaux seuils.
        """
        self._config = config
        if critical_score is not None:
            self._critical_score = critical_score

    def process(
        self,
        result: AnomalyResult,
        expected_traffic_mbps: float | None = None,
        expected_loss_percent: float | None = None,
        observed_traffic_mbps: float | None = None,
        observed_loss_percent: float | None = None,
    ) -> tuple[list[Alert], Incident | None]:
        alerts, incident = self._process(
            result,
            expected_traffic_mbps,
            expected_loss_percent,
            observed_traffic_mbps,
            observed_loss_percent,
        )
        return (alerts if self._alerts_enabled else []), incident

    def _process(
        self,
        result: AnomalyResult,
        expected_traffic_mbps: float | None = None,
        expected_loss_percent: float | None = None,
        observed_traffic_mbps: float | None = None,
        observed_loss_percent: float | None = None,
    ) -> tuple[list[Alert], Incident | None]:
        """
        Traite un AnomalyResult et fait évoluer l'état de la sortie.

        Renvoie (alertes générées à ce pas, incident touché — actif ou tout
        juste fermé). L'appelant doit persister cet incident s'il n'est pas
        None, y compris lorsqu'il vient de se fermer : une fois l'état
        interne réinitialisé, `active_incidents()` ne le renverra plus.
        """
        state = self._states.setdefault(result.output_id, _OutputState())

        if result.data_gap:
            alerts = self._handle_data_gap(result, state)
            return alerts, state.incident

        state.last_valid_timestamp = result.timestamp
        min_duration = self._config.minimum_duration_for(result.output_id)
        recovery_duration = self._config.recovery_duration_for(result.output_id)

        touched_incident = state.incident
        if state.incident is None:
            alerts = self._process_no_incident(result, state, min_duration, expected_traffic_mbps, expected_loss_percent, observed_traffic_mbps, observed_loss_percent)
            touched_incident = state.incident
        else:
            # `touched_incident` référence déjà l'objet muté en place, y compris
            # si state.incident a été remis à None (fermeture) pendant l'appel.
            alerts = self._process_active_incident(result, state, min_duration, recovery_duration, expected_traffic_mbps, expected_loss_percent, observed_traffic_mbps, observed_loss_percent)

        return alerts, touched_incident

    # -- Pas d'incident en cours ------------------------------------------------

    def _process_no_incident(
        self, result, state: _OutputState, min_duration, expected_traffic, expected_loss, observed_traffic, observed_loss
    ) -> list[Alert]:
        """Aucun incident en cours : seule l'entrée en SUSPECT peut se produire ici."""
        alerts: list[Alert] = []
        score = result.joint_anomaly_score

        if score < self._config.warning_score:
            return alerts

        state.suspect_since = result.timestamp
        incident = self._new_incident(result, expected_traffic, expected_loss, observed_traffic, observed_loss)
        incident.status = IncidentStatus.SUSPECT
        incident.suspected_at = result.timestamp
        state.incident = incident
        alerts.append(self._alert(incident, "suspect", result.timestamp, f"Signal faible détecté sur {result.output_id}"))
        return alerts

    # -- Incident actif (SUSPECT ou OPEN) ------------------------------------------

    def _process_active_incident(
        self, result, state: _OutputState, min_duration, recovery_duration, expected_traffic, expected_loss, observed_traffic, observed_loss
    ) -> list[Alert]:
        alerts: list[Alert] = []
        incident = state.incident
        assert incident is not None
        score = result.joint_anomaly_score

        if incident.status == IncidentStatus.STALE:
            # Une mesure valide reprend après un trou prolongé (§31) :
            # l'incident redevient OPEN, il n'est jamais resté "résolu".
            incident.status = IncidentStatus.OPEN

        self._append_evidence(incident, result, expected_traffic, expected_loss, observed_traffic, observed_loss)
        incident.last_seen_at = result.timestamp
        incident.score = max(incident.score, score)
        incident.current_score = score
        if result.traffic_deviation_percent is not None:
            incident.peak_traffic_deviation_percent = _peak(incident.peak_traffic_deviation_percent, result.traffic_deviation_percent)
        if observed_loss is not None:
            incident.peak_loss_percent = max(incident.peak_loss_percent, observed_loss)

        if incident.status == IncidentStatus.SUSPECT:
            if score < self._config.warning_score:
                incident.status = IncidentStatus.CLOSED
                incident.closed_at = result.timestamp
                alerts.append(self._alert(incident, "closed", result.timestamp, "Signal faible résorbé sans confirmation"))
                self._reset(state)
                return alerts

            if score >= self._config.open_score:
                elapsed = result.timestamp - state.suspect_since
                if elapsed >= min_duration:
                    incident.status = IncidentStatus.OPEN
                    incident.opened_at = result.timestamp
                    state.open_since = result.timestamp
                    incident.severity = self._severity_for(score)
                    incident.incident_type = _classify_type(
                        result.traffic_anomaly_score, result.loss_anomaly_score, result.traffic_deviation_percent
                    )
                    alerts.append(self._alert(incident, "opened", result.timestamp, self._describe(incident)))
            # Entre warning_score et open_score, ou open_score pas encore
            # tenu assez longtemps : reste en SUSPECT sans alerte supplémentaire.
            return alerts

        # status == OPEN (ou STALE traité séparément dans _handle_data_gap)
        if score >= self._config.open_score:
            state.below_close_since = None
            incident.severity = self._severity_for(score)
            alerts.append(self._alert(incident, "updated", result.timestamp, self._describe(incident)))
            return alerts

        if score < self._config.close_score:
            if state.below_close_since is None:
                state.below_close_since = result.timestamp
                alerts.append(self._alert(incident, "recovering", result.timestamp, "Retour progressif à la normale"))
            elif result.timestamp - state.below_close_since >= recovery_duration:
                incident.status = IncidentStatus.CLOSED
                incident.closed_at = result.timestamp
                alerts.append(self._alert(incident, "closed", result.timestamp, self._describe(incident)))
                self._reset(state)
            return alerts

        # Entre close_score et open_score : incident maintenu tel quel (hystérésis, §21).
        state.below_close_since = None
        return alerts

    # -- Trous de données (§31) --------------------------------------------------

    def _handle_data_gap(self, result: AnomalyResult, state: _OutputState) -> list[Alert]:
        """
        Une mesure MISSING ne fait ni progresser ni régresser le chrono de
        persistance (§31). Un incident OPEN reste OPEN, avec une preuve
        `data_gap=True`, jusqu'à une limite dure (`stale_after`) au-delà de
        laquelle il passe en STALE.
        """
        if state.incident is None or state.incident.status not in (IncidentStatus.OPEN, IncidentStatus.STALE):
            return []

        incident = state.incident
        incident.evidence.append(
            IncidentEvidence(
                timestamp=result.timestamp,
                traffic_anomaly_score=0.0,
                loss_anomaly_score=0.0,
                data_gap=True,
            )
        )

        gap_start = incident.last_seen_at
        if result.timestamp - gap_start > self._config.stale_after:
            if incident.status != IncidentStatus.STALE:
                incident.status = IncidentStatus.STALE
                return [self._alert(incident, "stale", result.timestamp, "Trou de données prolongé : incident marqué STALE")]
        return []

    # -- Aides internes ----------------------------------------------------------

    def _new_incident(self, result, expected_traffic, expected_loss, observed_traffic, observed_loss) -> Incident:
        incident = Incident(
            id=str(uuid.uuid4()),
            output_id=result.output_id,
            status=IncidentStatus.SUSPECT,
            severity=Severity.INFO,
            incident_type=IncidentType.TRAFFIC_DROP,  # affiné à l'ouverture (§23)
            last_seen_at=result.timestamp,
            score=result.joint_anomaly_score,
            current_score=result.joint_anomaly_score,
        )
        self._append_evidence(incident, result, expected_traffic, expected_loss, observed_traffic, observed_loss)
        return incident

    def _append_evidence(self, incident: Incident, result, expected_traffic, expected_loss, observed_traffic, observed_loss) -> None:
        incident.evidence.append(
            IncidentEvidence(
                timestamp=result.timestamp,
                traffic_mbps=observed_traffic,
                expected_traffic_mbps=expected_traffic,
                traffic_deviation_percent=result.traffic_deviation_percent,
                loss_percent=observed_loss,
                expected_loss_percent=expected_loss,
                traffic_anomaly_score=result.traffic_anomaly_score,
                loss_anomaly_score=result.loss_anomaly_score,
                data_gap=result.data_gap,
            )
        )

    def _severity_for(self, score: float) -> Severity:
        if score >= self._critical_score:
            return Severity.CRITICAL
        return Severity.WARNING

    def _describe(self, incident: Incident) -> str:
        return (
            f"{incident.incident_type.value} sur {incident.output_id} "
            f"(score max={incident.score:.2f}, actuel={self._current(incident):.2f}, "
            f"sévérité={incident.severity.value})"
        )

    @staticmethod
    def _current(incident: Incident) -> float:
        return incident.current_score if incident.current_score is not None else incident.score

    def _alert(self, incident: Incident, event: str, timestamp: datetime, message: str) -> Alert:
        severity = Severity.INFO if event in ("suspect", "recovering") else incident.severity
        return Alert(
            incident_id=incident.id,
            output_id=incident.output_id,
            severity=severity,
            event=AlertEvent(event),
            timestamp=timestamp,
            message=message,
        )

    def _reset(self, state: _OutputState) -> None:
        state.incident = None
        state.suspect_since = None
        state.open_since = None
        state.below_close_since = None

    def active_incidents(self) -> list[Incident]:
        return [s.incident for s in self._states.values() if s.incident is not None and s.incident.is_active]


def _peak(current: float, candidate: float | None) -> float:
    if candidate is None:
        return current
    return candidate if abs(candidate) > abs(current) else current
