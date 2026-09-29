"""
Modèles de données communs du système de surveillance réseau.

Ce module ne contient aucune logique métier : il définit uniquement les
contrats de données partagés entre les couches DataSource, ForecastModel,
AnomalyDetector et IncidentManager (cf. cahier des charges §5, §23, §24).

Choix d'implémentation : Pydantic v2 est utilisé plutôt que des
dataclasses simples, afin de bénéficier gratuitement de la validation
d'entrée (contraintes du §3) et d'une compatibilité directe avec FastAPI
pour l'exposition future d'une API (§37).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DataQualityFlag(str, Enum):
    """
    Drapeaux de qualité posés lors de la validation d'une mesure (§31).

    MISSING et TEMPORAL_GAP couvrent l'absence de donnée (respectivement
    une mesure ponctuelle manquante et un trou détecté dans la série).
    Les autres flags qualifient une donnée présente mais suspecte.
    """

    MISSING = "missing"
    TEMPORAL_GAP = "temporal_gap"
    DUPLICATE = "duplicate"
    NEGATIVE_TRAFFIC = "negative_traffic"
    LOSS_OUT_OF_RANGE = "loss_out_of_range"
    UNKNOWN_OUTPUT = "unknown_output"
    LATE_ARRIVAL = "late_arrival"


class ForecastModelName(str, Enum):
    """Modèles de forecasting disponibles en V1 (§10)."""

    SEASONAL_BASELINE = "seasonal_baseline"
    HOLT_WINTERS = "holt_winters"


class IncidentStatus(str, Enum):
    """
    États persistés du moteur d'incidents (§19, révisé pour inclure
    SUSPECT comme état à part entière et STALE pour les trous de données
    prolongés pendant un incident OPEN, §31).

    NORMAL n'est pas un état persisté : l'absence d'incident n'a pas
    besoin d'être matérialisée.
    """

    SUSPECT = "suspect"
    OPEN = "open"
    CLOSED = "closed"
    STALE = "stale"


class Severity(str, Enum):
    """Niveaux de sévérité d'un incident (§26)."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class IncidentType(str, Enum):
    """Types d'incidents initiaux V1 (§23)."""

    TRAFFIC_DROP = "traffic_drop"
    TRAFFIC_SPIKE = "traffic_spike"
    HIGH_LOSS = "high_loss"
    TRAFFIC_DROP_WITH_HIGH_LOSS = "traffic_drop_with_high_loss"
    PROGRESSIVE_DEGRADATION = "progressive_degradation"
    INTERMITTENT_DEGRADATION = "intermittent_degradation"


# ---------------------------------------------------------------------------
# Mesures brutes
# ---------------------------------------------------------------------------


class RawMeasurement(BaseModel):
    """
    Mesure brute non validée, telle que reçue de la source avant l'étape
    de validation (§4 "Data Validation", §31).

    Contrairement à TrafficSample, aucune contrainte de valeur n'est
    appliquée ici au-delà du typage : détecter une valeur négative ou une
    perte hors bornes est précisément le rôle de DataValidator, pas
    quelque chose que ce modèle doit empêcher de représenter.
    """

    timestamp: datetime
    output_id: str
    traffic_mbps: float
    loss_percent: float


class TrafficSample(BaseModel):
    """
    Format canonique d'une mesure (§3). Contrat strict et minimal :
    toute normalisation depuis une source réelle doit converger vers
    exactement ces quatre champs.
    """

    timestamp: datetime
    output_id: str = Field(min_length=1)
    traffic_mbps: float = Field(ge=0)
    loss_percent: float = Field(ge=0, le=100)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp doit être timezone-aware (§3)")
        return v


class ValidatedSample(BaseModel):
    """
    Résultat de l'étape de validation (§31), en amont du feature
    engineering. `sample` est None lorsque la donnée est totalement
    absente (trou temporel) plutôt qu'invalide mais présente.

    Ce modèle n'apparaît pas explicitement dans le cahier des charges mais
    est nécessaire pour porter les drapeaux qualité sans polluer le
    contrat canonique de TrafficSample.
    """

    output_id: str
    timestamp: datetime
    sample: TrafficSample | None = None
    quality_flags: list[DataQualityFlag] = Field(default_factory=list)

    # Drapeaux qui rendent la mesure inutilisable pour le forecasting et
    # la détection (par opposition à un flag purement informatif comme
    # LATE_ARRIVAL, qui n'empêche pas l'utilisation de la donnée).
    _BLOCKING_FLAGS = frozenset(
        {
            DataQualityFlag.MISSING,
            DataQualityFlag.TEMPORAL_GAP,
            DataQualityFlag.NEGATIVE_TRAFFIC,
            DataQualityFlag.LOSS_OUT_OF_RANGE,
            DataQualityFlag.UNKNOWN_OUTPUT,
        }
    )

    @property
    def is_usable(self) -> bool:
        if self.sample is None:
            return False
        return not self._BLOCKING_FLAGS.intersection(self.quality_flags)


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------


class ForecastResult(BaseModel):
    """Résultat d'une prévision de trafic pour un horizon donné (§13)."""

    output_id: str
    timestamp: datetime
    predicted_traffic_mbps: float = Field(ge=0)
    lower_bound_mbps: float = Field(ge=0)
    upper_bound_mbps: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    model_name: ForecastModelName

    @model_validator(mode="after")
    def bounds_are_consistent(self) -> ForecastResult:
        if not (self.lower_bound_mbps <= self.predicted_traffic_mbps <= self.upper_bound_mbps):
            raise ValueError(
                "l'intervalle prédictif doit vérifier "
                "lower_bound_mbps <= predicted_traffic_mbps <= upper_bound_mbps"
            )
        return self

    @property
    def interval_width_mbps(self) -> float:
        """Largeur de l'intervalle attendu, utilisée pour normaliser le score (§16.1)."""
        return self.upper_bound_mbps - self.lower_bound_mbps


# ---------------------------------------------------------------------------
# Détection d'anomalies
# ---------------------------------------------------------------------------


class AnomalyResult(BaseModel):
    """
    Sortie combinée de la détection statistique (§14-§16).

    Plusieurs scores explicites sont conservés plutôt qu'un score opaque
    unique, conformément au §16. La normalisation retenue (§16.1) est
    fondée sur la position de l'observation par rapport à l'intervalle
    prédictif ou à la dispersion historique robuste, pas sur un z-score
    classique.
    """

    output_id: str
    timestamp: datetime

    traffic_anomaly_score: float = Field(ge=0, le=1)
    loss_anomaly_score: float = Field(ge=0, le=1)
    persistence_score: float = Field(ge=0, le=1, default=0.0)
    joint_anomaly_score: float = Field(ge=0, le=1)

    traffic_deviation_percent: float | None = None
    loss_deviation_points: float | None = None
    traffic_out_of_interval: bool = False

    data_gap: bool = False
    """Mesure manquante au moment du calcul (§31) : les scores ci-dessus
    reflètent alors une donnée imputée ou une absence de signal, pas une
    observation réelle."""


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


class IncidentEvidence(BaseModel):
    """
    Une preuve horodatée justifiant l'état d'un incident (§24).

    Objectif explicite : rendre chaque incident compréhensible sans
    connaître le fonctionnement interne du modèle (§28).
    """

    timestamp: datetime

    traffic_mbps: float | None = None
    expected_traffic_mbps: float | None = None
    traffic_deviation_percent: float | None = None

    loss_percent: float | None = None
    expected_loss_percent: float | None = None

    traffic_anomaly_score: float = Field(ge=0, le=1)
    loss_anomaly_score: float = Field(ge=0, le=1)

    data_gap: bool = False
    """Trou de données pendant un incident OPEN (§31) : le chrono de
    persistance est alors suspendu, pas réinitialisé, pour ce point."""


class Incident(BaseModel):
    """
    Cycle de vie complet d'un incident (§19-§23).

    `suspected_at` marque l'entrée en état SUSPECT (score >= warning_score,
    §19) tandis que `opened_at` marque le franchissement effectif
    d'open_score pendant minimum_duration (§20). Cette distinction est
    nécessaire pour que `duration_seconds` reflète la durée réelle de
    l'incident confirmé, pas la durée depuis le premier signal faible.
    """

    id: str
    output_id: str

    status: IncidentStatus
    severity: Severity
    incident_type: IncidentType

    suspected_at: datetime | None = None
    opened_at: datetime | None = None
    last_seen_at: datetime
    closed_at: datetime | None = None

    score: float = Field(ge=0, le=1)
    """PIC du score joint depuis l'ouverture de l'incident : ne redescend jamais
    tant que l'incident est actif. À ne pas confondre avec `current_score`."""

    current_score: float | None = Field(default=None, ge=0, le=1)
    """Score joint du DERNIER pas de temps traité. `severity` est recalculée à
    partir de cette valeur (elle peut donc redescendre), alors que `score` est un
    pic : afficher les deux évite l'apparente contradiction « score 0.90 mais
    sévérité warning » (PLAN_CORRECTION.md §3.6). `None` pour un incident
    persisté avant l'introduction de ce champ."""

    peak_traffic_deviation_percent: float = 0.0
    peak_loss_percent: float = 0.0

    description: str = ""
    evidence: list[IncidentEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> Incident:
        if self.opened_at is not None and self.suspected_at is not None:
            if self.opened_at < self.suspected_at:
                raise ValueError("opened_at ne peut pas précéder suspected_at")
        if self.closed_at is not None and self.opened_at is not None:
            if self.closed_at < self.opened_at:
                raise ValueError("closed_at ne peut pas précéder opened_at")
        return self

    @property
    def duration_seconds(self) -> float | None:
        """Durée depuis l'ouverture effective (opened_at), pas depuis SUSPECT."""
        if self.opened_at is None:
            return None
        end = self.closed_at or self.last_seen_at
        return (end - self.opened_at).total_seconds()

    @property
    def is_active(self) -> bool:
        """Un incident SUSPECT, OPEN ou STALE nécessite encore un suivi actif."""
        return self.status in (IncidentStatus.SUSPECT, IncidentStatus.OPEN, IncidentStatus.STALE)


# ---------------------------------------------------------------------------
# Alertes
# ---------------------------------------------------------------------------


class AlertEvent(str, Enum):
    """Événement du cycle de vie d'un incident qui déclenche une alerte (§27)."""

    SUSPECT = "suspect"
    OPENED = "opened"
    UPDATED = "updated"
    RECOVERING = "recovering"
    CLOSED = "closed"
    STALE = "stale"


class Alert(BaseModel):
    """Une alerte, toujours liée à un événement du moteur d'incidents (§27)."""

    incident_id: str
    output_id: str
    severity: Severity
    event: AlertEvent
    timestamp: datetime
    message: str
