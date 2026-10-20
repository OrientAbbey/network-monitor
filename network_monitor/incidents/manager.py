"""
Moteur d'incidents (§19-§27) : transforme des scores d'anomalie ponctuels
en incidents avec hystérésis (§21), sans jamais dépendre d'un algorithme de
détection particulier (§4).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from network_monitor.app.config import IncidentConfig
from network_monitor.app.models import (
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
    below_warning_since: datetime | None = None
    suspect_steps_above: int = 0
    last_closed_incident: Incident | None = None
    last_closed_at: datetime | None = None
    """Dernier incident confirmé (opened_at non None) fermé pour cette sortie,
    conservé pendant `merge_window` (§22) pour rouvrir le même incident si une
    nouvelle anomalie survient peu après, plutôt que d'en ouvrir un autre."""


def incident_worth_persisting(incident: Incident) -> bool:
    """
    Un incident qui n'a jamais franchi open_score est un signal faible qui
    s'est résorbé seul (§19) — il n'apporte rien à l'historique et ne doit
    pas être écrit en base : sur 7 jours de trafic normal, c'est ~99% des
    lignes générées sans ce filtre. `opened_at` reste renseigné même après
    fermeture, donc ce test reste vrai pour un incident confirmé qui vient
    de se refermer (il doit, lui, être persisté).
    """
    return incident.opened_at is not None


def _classify_type(traffic_score: float, loss_score: float, traffic_deviation_pct: float | None) -> IncidentType:
    """Type « instantané » d'un incident, d'après son signal le plus marqué (§23, §25)."""
    traffic_relevant = traffic_score > 0.3
    loss_relevant = loss_score > 0.3
    rising_traffic = (traffic_deviation_pct or 0.0) > 0

    if traffic_relevant and loss_relevant:
        return IncidentType.CONGESTION if rising_traffic else IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS
    if traffic_relevant and rising_traffic:
        return IncidentType.TRAFFIC_SPIKE
    if traffic_relevant:
        return IncidentType.TRAFFIC_DROP
    return IncidentType.HIGH_LOSS


# Seuils du motif temporel (§23), choisis avec le banc d'évaluation
# (`analysis/evaluation.py`, exactitude des types sur scénarios injectés).
_PATTERN_MIN_POINTS = 6
_PATTERN_MIN_PEAK = 20.0  # magnitude sous laquelle l'écart ressemble à du bruit
_LOSS_TO_DEVIATION = 8.0  # 1 point de pertes vaut 8 points d'écart de trafic
_HIGH_FRACTION, _LOW_FRACTION = 0.6, 0.3  # hystérésis actif/inactif, en fraction du pic
_MIN_SWITCHES = 3
_MIN_RAMP_CORRELATION = 0.8
_MIN_RAMP_RISE = 0.3  # montée minimale, en fraction du pic


def _pattern_type(evidence: list[IncidentEvidence]) -> IncidentType | None:
    """
    Motif dans le temps d'un incident : alternance (intermittent) ou montée
    régulière de l'écart (dégradation progressive). Travaille sur la
    *magnitude* de l'écart, pas sur les scores, qui saturent à 1 dès que
    l'écart est net et cachent alors toute évolution ; l'alternance est
    détectée avec une hystérésis (actif au-dessus de 60 % du pic, inactif
    sous 30 %) pour que le bruit autour d'un seuil unique ne l'imite pas.
    """
    m = [
        max(abs(e.traffic_deviation_percent or 0.0), _LOSS_TO_DEVIATION * (e.loss_percent or 0.0))
        for e in evidence
        if not e.data_gap
    ]
    if len(m) < _PATTERN_MIN_POINTS:
        return None
    peak = float(np.percentile(m, 90))
    if peak < _PATTERN_MIN_PEAK:
        return None

    state: bool | None = None
    switches = 0
    for v in m:
        if v >= _HIGH_FRACTION * peak and state is not True:
            switches += state is False
            state = True
        elif v <= _LOW_FRACTION * peak and state is not False:
            switches += state is True
            state = False
    if switches >= _MIN_SWITCHES:
        return IncidentType.INTERMITTENT_DEGRADATION

    x = np.arange(len(m), dtype=float)
    if np.std(m) > 0:
        slope = float(np.polyfit(x, m, 1)[0])
        corr = float(np.corrcoef(x, m)[0, 1])
        if corr >= _MIN_RAMP_CORRELATION and slope * (len(m) - 1) >= _MIN_RAMP_RISE * peak:
            return IncidentType.PROGRESSIVE_DEGRADATION
    return None


def _incident_type(evidence: list[IncidentEvidence]) -> IncidentType:
    """Type d'un incident : motif temporel s'il y en a un, sinon signal le plus marqué."""
    pattern = _pattern_type(evidence)
    if pattern is not None:
        return pattern
    real = [e for e in evidence if not e.data_gap]
    if not real:
        return IncidentType.TRAFFIC_DROP
    # Valeurs médianes : un seul pas bruité ne doit pas suffire à ajouter « pertes »
    # (ou « trafic ») au type d'un incident dont ce n'est pas la nature.
    deviations = [e.traffic_deviation_percent for e in real if e.traffic_deviation_percent is not None]
    return _classify_type(
        float(np.median([e.traffic_anomaly_score for e in real])),
        float(np.median([e.loss_anomaly_score for e in real])),
        float(np.median(deviations)) if deviations else None,
    )


@dataclass
class Alert:
    """
    Une alerte, toujours liée à un événement du moteur d'incidents (§27).

    Porte directement le contenu attendu par §27/§28 (type, durée, trafic
    attendu/observé, pertes) pour rester lisible sans consulter l'incident :
    "compréhensible sans connaître le fonctionnement interne du modèle" (§28).
    """

    incident_id: str
    output_id: str
    severity: Severity
    event: str  # "suspect", "opened", "updated", "recovering", "closed", "stale", "reopened"
    timestamp: datetime
    message: str
    incident_type: IncidentType
    duration_seconds: float | None = None
    traffic_mbps: float | None = None
    expected_traffic_mbps: float | None = None
    traffic_deviation_percent: float | None = None
    loss_percent: float | None = None
    expected_loss_percent: float | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    confirmed: bool = True
    """False pour un signal faible résorbé sans jamais avoir été confirmé : à ne notifier à personne."""
    suppressed: str | None = None
    """« maintenance » ou « inhibited » : l'alerte est enregistrée mais jamais envoyée."""
    root_cause_incident_id: str | None = None
    impacted_outputs: list[str] = field(default_factory=list)
    """Sorties aval déjà touchées par ce même incident amont (alerte d'ouverture seulement)."""


class IncidentManager:
    """
    Gestionnaire du cycle de vie des incidents (§19-§27), à états
    SUSPECT / OPEN / STALE / CLOSED (§19, révisé). Indépendant des
    algorithmes de detection : ne consomme que des `AnomalyResult`.
    """

    def __init__(
        self, config: IncidentConfig, critical_score: float = 0.80, isolation_confirm_score: float = 1.01
    ) -> None:
        self._config = config
        self._critical_score = critical_score
        # > 1 par défaut : sans Isolation Forest activé, aucun score ne l'atteint.
        self._isolation_confirm_score = isolation_confirm_score
        self._states: dict[str, _OutputState] = {}
        self._parents: dict[str, str] = {}
        self._correlation_window = timedelta(minutes=15)
        self._in_maintenance: Callable[[str, datetime], bool] = lambda _output, _at: False
        # Incidents dont les alertes sont retenues (aucun envoi) : id → « maintenance » | « inhibited ».
        self._silenced: dict[str, str] = {}
        self._open_ids: set[str] = set()  # incidents confirmés et pas encore fermés (cause amont encore active ?)

    def set_topology(self, parents: dict[str, str], correlation_window: timedelta) -> None:
        """Dépendances amont/aval (`topology.parents`, clé = aval, valeur = amont)."""
        self._parents = dict(parents)
        self._correlation_window = correlation_window

    def set_maintenance_check(self, check: Callable[[str, datetime], bool]) -> None:
        """Fonction `(sortie, instant) -> bool` : la sortie est-elle en maintenance planifiée à cet instant ?"""
        self._in_maintenance = check

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
            return self._finalize(result, state.incident, alerts), state.incident

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

        return self._finalize(result, touched_incident, alerts), touched_incident

    # -- Maintenance et dépendances entre sorties -------------------------------------

    def _descendants(self, output_id: str) -> list[str]:
        out, frontier = [], [output_id]
        while frontier:
            node = frontier.pop()
            kids = [c for c, p in self._parents.items() if p == node]
            out += kids
            frontier += kids
        return out

    def _active(self, output_id: str) -> Incident | None:
        state = self._states.get(output_id)
        inc = state.incident if state else None
        return inc if inc is not None and inc.status == IncidentStatus.OPEN else None

    def _related(self, a: Incident, b: Incident) -> bool:
        """Débuts réels assez proches pour être une seule cause (`correlation_window`)."""
        start_a, start_b = a.started_at or a.opened_at, b.started_at or b.opened_at
        return start_a is not None and start_b is not None and abs(start_a - start_b) <= self._correlation_window

    def _correlate(self, incident: Incident, at: datetime) -> tuple[str | None, list[str]]:
        """À la confirmation d'un incident : retenu par une maintenance ? rattaché à une cause amont ?
        quelles sorties aval déjà touchées explique-t-il ? Renvoie (raison de retenue, sorties aval)."""
        silenced: str | None = None
        if self._in_maintenance(incident.output_id, at):
            incident.maintenance, silenced = True, "maintenance"
        ancestor = self._parents.get(incident.output_id)
        while ancestor is not None:
            root = self._active(ancestor)
            if root is not None and root.id != incident.id and self._related(root, incident):
                incident.root_cause_incident_id = root.id
                if silenced is None:
                    incident.inhibited, silenced = True, "inhibited"
                break
            ancestor = self._parents.get(ancestor)
        impacted = []
        for child in self._descendants(incident.output_id):
            other = self._active(child)
            if other is not None and other.root_cause_incident_id is None and self._related(incident, other):
                other.root_cause_incident_id = incident.id
                impacted.append(child)
        return silenced, impacted

    def _finalize(self, result: AnomalyResult, incident: Incident | None, alerts: list[Alert]) -> list[Alert]:
        """Applique aux alertes du pas la maintenance et l'inhibition (retenir, jamais supprimer : une
        alerte retenue reste enregistrée et visible, elle n'est simplement pas envoyée), et libère
        ce qui doit l'être (maintenance terminée, cause amont résolue)."""
        extra: list[Alert] = []
        for alert in alerts:  # suivi des incidents confirmés encore actifs, toutes sorties confondues
            if alert.confirmed and alert.event in ("opened", "reopened"):
                self._open_ids.add(alert.incident_id)
            elif alert.confirmed and alert.event == "closed":
                self._open_ids.discard(alert.incident_id)
        if incident is not None and incident.opened_at is not None:
            for alert in alerts:
                if alert.incident_id == incident.id and alert.event in ("opened", "reopened"):
                    silenced, impacted = self._correlate(incident, result.timestamp)
                    if silenced:
                        self._silenced[incident.id] = silenced
                    alert.impacted_outputs = impacted
                    break
            held = self._silenced.get(incident.id)
            state = self._states.get(incident.output_id)
            still_open = incident.status == IncidentStatus.OPEN
            if held == "maintenance" and still_open and not self._in_maintenance(incident.output_id, result.timestamp):
                # La maintenance est terminée et l'incident dure : c'est maintenant une vraie alerte.
                del self._silenced[incident.id]
                extra.append(
                    self._alert(incident, "opened", result.timestamp, f"Incident persistant après la fin de la maintenance sur {incident.output_id}")
                )
            elif (
                held == "inhibited" and still_open
                and incident.root_cause_incident_id not in self._open_ids
                and state is not None and state.below_close_since is None  # pas en cours de retour à la normale
            ):
                # La cause amont est résolue et cette sortie est toujours en panne : elle n'est plus « expliquée ».
                del self._silenced[incident.id]
                extra.append(self._alert(incident, "opened", result.timestamp, "Incident persistant après la résolution de la cause amont"))
            alerts_for_incident = [a for a in alerts + extra if a.incident_id == incident.id]
            for alert in alerts_for_incident:
                alert.suppressed = self._silenced.get(incident.id)
                alert.root_cause_incident_id = incident.root_cause_incident_id
            if any(a.event == "closed" and a.confirmed for a in alerts_for_incident):
                self._silenced.pop(incident.id, None)
        return alerts + extra

    # -- Pas d'incident en cours ------------------------------------------------

    def _process_no_incident(
        self, result, state: _OutputState, min_duration, expected_traffic, expected_loss, observed_traffic, observed_loss
    ) -> list[Alert]:
        """Aucun incident en cours : entrée en SUSPECT, ou réouverture (§22)."""
        alerts: list[Alert] = []
        score = result.joint_anomaly_score

        if score < self._config.warning_score:
            return alerts

        state.suspect_since = result.timestamp
        state.suspect_steps_above = 1
        state.below_warning_since = None
        incident = self._new_incident(result, expected_traffic, expected_loss, observed_traffic, observed_loss)
        incident.status = IncidentStatus.SUSPECT
        incident.suspected_at = result.timestamp
        state.incident = incident
        alerts.append(self._alert(incident, "suspect", result.timestamp, f"Signal faible détecté sur {result.output_id}"))
        return alerts

    def _try_reopen(self, state: _OutputState, episode_start: datetime) -> Incident | None:
        """Rouvre le dernier incident confirmé de cette sortie si le nouvel épisode a commencé
        moins de `merge_window` après sa fin (§22 : un incident intermittent reste un seul
        incident, pas une suite d'incidents distincts). N'est appelé qu'à la **confirmation**
        du nouvel épisode : un pic de bruit juste après la fin d'un incident ne le prolonge pas."""
        closed, closed_at = state.last_closed_incident, state.last_closed_at
        if closed is None or closed_at is None or episode_start - closed_at > self._config.merge_window:
            return None
        state.last_closed_incident = None
        state.last_closed_at = None
        closed.status = IncidentStatus.OPEN
        closed.closed_at = None
        closed.ended_at = None
        return closed

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
        if result.traffic_deviation_percent is not None:
            incident.peak_traffic_deviation_percent = _peak(incident.peak_traffic_deviation_percent, result.traffic_deviation_percent)
        if observed_loss is not None:
            incident.peak_loss_percent = max(incident.peak_loss_percent, observed_loss)

        if incident.status == IncidentStatus.SUSPECT:
            if score < self._config.warning_score:
                # Un creux isolé ne referme pas le SUSPECT : un incident intermittent
                # (§22) retombe sous le seuil à chaque période et doit rester un seul
                # incident. Le SUSPECT ne se referme qu'après `recovery_duration` de calme.
                if state.below_warning_since is None:
                    state.below_warning_since = result.timestamp
                # Un pic isolé (moins de `min_suspect_steps` pas au-dessus du seuil) ne
                # bénéficie d'aucune tolérance : c'est le bruit ordinaire de la mesure.
                if (
                    state.suspect_steps_above < self._config.min_suspect_steps
                    or result.timestamp - state.below_warning_since >= recovery_duration
                ):
                    incident.status = IncidentStatus.CLOSED
                    incident.closed_at = result.timestamp
                    alerts.append(self._alert(incident, "closed", result.timestamp, "Signal faible résorbé sans confirmation"))
                    self._reset(state, incident)
                return alerts

            state.below_warning_since = None
            state.suspect_steps_above += 1
            if score >= self._config.open_score:
                elapsed = result.timestamp - state.suspect_since
                if elapsed >= min_duration:
                    previous = self._try_reopen(state, incident.suspected_at or result.timestamp)
                    if previous is not None:
                        # Le nouvel épisode confirmé prolonge l'incident précédent (même identité,
                        # début d'origine conservé) : on lui verse les preuves du SUSPECT abandonné.
                        previous.evidence.extend(incident.evidence)
                        self._trim_evidence(previous)
                        previous.last_seen_at = result.timestamp
                        previous.score = max(previous.score, incident.score)
                        previous.peak_traffic_deviation_percent = _peak(
                            previous.peak_traffic_deviation_percent, incident.peak_traffic_deviation_percent
                        )
                        previous.peak_loss_percent = max(previous.peak_loss_percent, incident.peak_loss_percent)
                        previous.incident_type = _incident_type(previous.evidence)
                        state.incident, state.open_since = previous, result.timestamp
                        alerts.append(
                            self._alert(
                                previous, "reopened", result.timestamp,
                                f"Incident ré-ouvert sur {result.output_id} (nouvel épisode confirmé dans la fenêtre de fusion)",
                            )
                        )
                        return alerts
                    incident.status = IncidentStatus.OPEN
                    incident.opened_at = result.timestamp
                    # Rétro-daté : l'incident a commencé au premier signal faible de l'épisode.
                    incident.started_at = incident.suspected_at
                    state.open_since = result.timestamp
                    incident.severity = self._severity_for(score)
                    incident.incident_type = _incident_type(incident.evidence)
                    alerts.append(self._alert(incident, "opened", result.timestamp, self._describe(incident)))
            # Entre warning_score et open_score, ou open_score pas encore
            # tenu assez longtemps : reste en SUSPECT sans alerte supplémentaire.
            return alerts

        # status == OPEN (ou STALE traité séparément dans _handle_data_gap)
        # Le motif (progressif, intermittent) ne se voit qu'avec du recul : le
        # type est réévalué à chaque pas sur l'ensemble des preuves.
        # Figé pendant le retour à la normale : les pas de récupération ne décrivent
        # plus l'incident (une rampe suivie d'une chute n'est plus une rampe).
        new_type = _incident_type(incident.evidence) if score >= self._config.close_score else incident.incident_type
        type_changed = new_type != incident.incident_type
        incident.incident_type = new_type
        if score >= self._config.open_score:
            state.below_close_since = None
            new_severity = self._severity_for(score)
            severity_changed = new_severity != incident.severity
            incident.severity = new_severity
            # Anti-spam (§27) : une alerte par événement du moteur, pas une par
            # tick — seul un changement de sévérité ou de type justifie une mise à jour.
            if severity_changed or type_changed:
                alerts.append(self._alert(incident, "updated", result.timestamp, self._describe(incident)))
            return alerts

        if score < self._config.close_score:
            if state.below_close_since is None:
                state.below_close_since = result.timestamp
                alerts.append(self._alert(incident, "recovering", result.timestamp, "Retour progressif à la normale"))
            elif result.timestamp - state.below_close_since >= recovery_duration:
                incident.status = IncidentStatus.CLOSED
                incident.closed_at = result.timestamp
                # Rétro-daté : la fin est le premier pas sous close_score, pas la décision de fermer.
                incident.ended_at = state.below_close_since
                alerts.append(self._alert(incident, "closed", result.timestamp, self._describe(incident)))
                self._reset(state, incident)
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
        if result.timestamp - gap_start > self._config.stale_after and incident.status != IncidentStatus.STALE:
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
        )
        self._append_evidence(incident, result, expected_traffic, expected_loss, observed_traffic, observed_loss)
        return incident

    _MAX_EVIDENCE = 200
    """Plafond du nombre de preuves conservées par incident (Perf-6) : un
    incident qui reste OPEN pendant des jours ne doit pas faire croître le
    blob `evidence` sans limite. Au-delà, on garde le début (contexte
    d'ouverture) et la fin (état récent), pas le milieu."""

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
                isolation_score=result.isolation_score,
                data_gap=result.data_gap,
            )
        )
        self._trim_evidence(incident)

    def _trim_evidence(self, incident: Incident) -> None:
        if len(incident.evidence) > self._MAX_EVIDENCE:
            head = 20
            incident.evidence = incident.evidence[:head] + incident.evidence[-(self._MAX_EVIDENCE - head):]

    def _severity_for(self, score: float) -> Severity:
        if score >= self._critical_score:
            return Severity.CRITICAL
        return Severity.WARNING

    def _describe(self, incident: Incident) -> str:
        text = (
            f"{incident.incident_type.value} sur {incident.output_id} "
            f"(score={incident.score:.2f}, sévérité={incident.severity.value})"
        )
        peak = max((e.isolation_score or 0.0 for e in incident.evidence), default=0.0)
        if peak >= self._isolation_confirm_score:
            text += f", confirmé par Isolation Forest ({peak:.2f})"
        return text

    def _alert(self, incident: Incident, event: str, timestamp: datetime, message: str) -> Alert:
        severity = Severity.INFO if event in ("suspect", "recovering") else incident.severity
        ev = incident.evidence[-1] if incident.evidence else None
        start = incident.started_at or incident.opened_at or incident.suspected_at
        # Une alerte de fermeture rapporte la durée réelle (début → fin rétro-datés), pas le moment de la décision.
        end = incident.ended_at if event == "closed" and incident.ended_at else timestamp
        return Alert(
            incident_id=incident.id,
            output_id=incident.output_id,
            severity=severity,
            event=event,
            timestamp=timestamp,
            message=message,
            incident_type=incident.incident_type,
            duration_seconds=(end - start).total_seconds() if start else None,
            traffic_mbps=ev.traffic_mbps if ev else None,
            expected_traffic_mbps=ev.expected_traffic_mbps if ev else None,
            traffic_deviation_percent=ev.traffic_deviation_percent if ev else None,
            loss_percent=ev.loss_percent if ev else None,
            expected_loss_percent=ev.expected_loss_percent if ev else None,
            started_at=incident.started_at,
            ended_at=incident.ended_at,
            confirmed=incident.opened_at is not None,
        )

    def _reset(self, state: _OutputState, closed_incident: Incident | None = None) -> None:
        if closed_incident is not None and closed_incident.opened_at is not None:
            # Incident confirmé (pas un simple signal faible résorbé) :
            # mémorisé pour une éventuelle réouverture (§22, merge_window).
            state.last_closed_incident = closed_incident
            state.last_closed_at = closed_incident.ended_at or closed_incident.closed_at
        state.incident = None
        state.below_warning_since = None
        state.suspect_steps_above = 0
        state.suspect_since = None
        state.open_since = None
        state.below_close_since = None

    def is_recovering(self, output_id: str) -> bool:
        """Vrai si l'incident en cours de cette sortie est déjà revenu sous `close_score` (retour à la normale à confirmer).
        Cet état n'existe qu'en mémoire : après un redémarrage, il est inconnu (donc faux)."""
        state = self._states.get(output_id)
        return state is not None and state.incident is not None and state.below_close_since is not None

    def active_incidents(self) -> list[Incident]:
        return [s.incident for s in self._states.values() if s.incident is not None and s.incident.is_active]


def _peak(current: float, candidate: float | None) -> float:
    if candidate is None:
        return current
    return candidate if abs(candidate) > abs(current) else current
