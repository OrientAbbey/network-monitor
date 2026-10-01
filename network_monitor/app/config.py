"""
Configuration du système (§35). Chargée via `yaml.safe_load` uniquement
(jamais `yaml.load`), pour empêcher l'exécution de code arbitraire depuis
un fichier de configuration non fiable.

Chaque champ de ce module doit avoir un effet observable dans le code : un
champ qui n'est lu nulle part induit l'opérateur en erreur (il croit pouvoir
changer un comportement qui ne bougera pas) et n'est pas conservé ici.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

_DURATION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([a-z]+)$")

_DURATION_UNITS = {
    "s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds", "seconds": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
}


def _parse_duration(value: str | float) -> timedelta:
    """Convertit '5m', '30d', '10 minutes', '2days' etc. en timedelta.

    Accepte aussi un nombre brut de secondes (int/float), et un séparateur
    optionnel (espace ou aucun) entre la quantité et l'unité.
    """
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    text = str(value).strip().lower()
    match = _DURATION_RE.match(text)
    if match is None:
        raise ValueError(f"Durée non reconnue : {value!r}")
    number, suffix = match.groups()
    unit = _DURATION_UNITS.get(suffix)
    if unit is None:
        raise ValueError(f"Unité de durée non reconnue : {value!r}")
    return timedelta(**{unit: float(number)})


class ModelSelectionConfig(BaseModel):
    minimum_history: timedelta = timedelta(days=45)
    validation_window: timedelta = timedelta(days=7)

    @field_validator("minimum_history", "validation_window", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v


class LossDetectionConfig(BaseModel):
    enabled: bool = True
    loss_std_floor: float = Field(default=0.05, ge=0.0)


class IsolationForestConfig(BaseModel):
    """Détecteur optionnel de confirmation (§17). Ne décide jamais seul
    d'ouvrir un incident : son score enrichit les preuves, rien d'autre."""

    enabled: bool = False
    n_estimators: int = Field(default=100, ge=10, le=1000)
    confirm_score: float = Field(default=0.6, ge=0.0, le=1.0)
    """Score normalisé (0 = normal, 1 = très isolé) à partir duquel un
    incident est décrit comme « confirmé » par l'Isolation Forest."""


class ExtendedFeaturesConfig(BaseModel):
    enabled: bool = False


class DetectionConfig(BaseModel):
    """Paramètres de la détection statistique (§17-§20). La détection du
    trafic n'est jamais désactivable indépendamment : sans elle il n'y a
    plus de détection du tout. Seule la composante pertes l'est (`loss.enabled`)."""

    loss: LossDetectionConfig = LossDetectionConfig()
    isolation_forest: IsolationForestConfig = IsolationForestConfig()
    extended_features: ExtendedFeaturesConfig = ExtendedFeaturesConfig()


class IncidentOutputOverride(BaseModel):
    minimum_duration: timedelta | None = None
    recovery_duration: timedelta | None = None

    @field_validator("minimum_duration", "recovery_duration", mode="before")
    @classmethod
    def _parse(cls, v):
        if v is None or isinstance(v, timedelta):
            return v
        return _parse_duration(v)


class IncidentConfig(BaseModel):
    open_score: float = Field(default=0.75, ge=0.0, le=1.0)
    close_score: float = Field(default=0.40, ge=0.0, le=1.0)
    warning_score: float = Field(default=0.50, ge=0.0, le=1.0)
    minimum_duration: timedelta = timedelta(minutes=15)
    recovery_duration: timedelta = timedelta(minutes=5)
    """Réglages (`minimum_duration`, `recovery_duration`, `min_suspect_steps`) choisis par
    balayage sur le Mock (`python -m network_monitor.analysis.evaluation --sweep`) : 8 jeux,
    96 scénarios, 162 sorties-jours. Par rapport à l'ancien réglage (10 min / 10 min / 2) :
    faux incidents 51 → 7 (0,31 → 0,04 par sortie et par jour), scénarios détectés 94 → 87
    sur 96 (les 9 manqués sont tous des intermittents), délai d'ouverture ≈ 16 → ≈ 20 min.
    `open_score` (0,65-0,85) et `close_score` (0,3-0,5) changent au plus de 3 faux incidents
    sur l'ensemble ; `warning_score` n'a pas été balayé. À re-mesurer sur des données réelles :
    le Mock ne contient que des anomalies nettes."""
    stale_after: timedelta = timedelta(minutes=15)
    merge_window: timedelta = timedelta(minutes=20)
    min_suspect_steps: int = Field(default=3, ge=1, le=12)
    """Nombre de pas au-dessus de `warning_score` avant qu'un SUSPECT tolère des creux
    (incident intermittent, §22). En dessous, un creux le referme aussitôt : un ou deux
    pics isolés sont le bruit ordinaire de la mesure. Mesuré sur le Mock (8 jeux, 96
    scénarios) : à 2, 94 scénarios détectés et 37 faux incidents ; à 3, 87 détectés et 10
    faux incidents — la différence est faite des intermittents dont un seul pas sur trois
    tombe dans la fenêtre « actif », que le pas de 5 min ne distingue pas du bruit."""
    """Une nouvelle anomalie survenant dans cette fenêtre après la fermeture
    d'un incident sur la même sortie rouvre ce même incident au lieu d'en
    créer un nouveau (§22 : un incident intermittent doit rester un seul
    incident)."""
    per_output_overrides: dict[str, IncidentOutputOverride] = Field(default_factory=dict)

    @field_validator("minimum_duration", "recovery_duration", "stale_after", "merge_window", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> IncidentConfig:
        # §21 : les trois seuils doivent être strictement ordonnés, sans quoi
        # un incident peut s'ouvrir sans être jamais passé par l'état SUSPECT
        # (ou l'inverse : un score "warning" au-dessus du score d'ouverture).
        if not (self.close_score < self.warning_score < self.open_score):
            raise ValueError(
                "les seuils d'incident doivent vérifier close_score < warning_score < "
                f"open_score (reçu close={self.close_score}, warning={self.warning_score}, "
                f"open={self.open_score})"
            )
        return self

    def minimum_duration_for(self, output_id: str) -> timedelta:
        override = self.per_output_overrides.get(output_id)
        if override and override.minimum_duration is not None:
            return override.minimum_duration
        return self.minimum_duration

    def recovery_duration_for(self, output_id: str) -> timedelta:
        override = self.per_output_overrides.get(output_id)
        if override and override.recovery_duration is not None:
            return override.recovery_duration
        return self.recovery_duration


class AlertsConfig(BaseModel):
    enabled: bool = True
    critical_score: float = Field(default=0.80, ge=0.0, le=1.0)


class ForecastConfig(BaseModel):
    models: list[str] = Field(default_factory=lambda: ["seasonal_baseline", "holt_winters"])
    """Modèles candidats pour la sélection automatique (§8). Retirer un nom de
    cette liste l'exclut de la sélection, même s'il resterait autrement le
    meilleur candidat."""


class HistoryConfig(BaseModel):
    minimum: timedelta = timedelta(days=30)
    preferred: timedelta = timedelta(days=90)

    @field_validator("minimum", "preferred", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v


class RecalibrationConfig(BaseModel):
    """Recalibration périodique des modèles de prévision (§32)."""

    enabled: bool = True
    interval: timedelta = timedelta(hours=24)

    @field_validator("interval", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v


class MonitoringConfig(BaseModel):
    interval: timedelta = timedelta(minutes=5)

    @field_validator("interval", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v


_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SourceConfig(BaseModel):
    """Source de données (§6) : Mock (défaut), rejeu CSV, ou table SQL."""

    type: Literal["mock", "csv", "sql"] = "mock"
    path: str | None = None
    url: SecretStr | None = None
    """URL SQLAlchemy (peut contenir un mot de passe) : jamais renvoyée en
    clair par GET /config."""
    table: str = "traffic_measurements"
    columns: dict[str, str] = Field(
        default_factory=lambda: {
            "timestamp": "timestamp",
            "output_id": "output_id",
            "traffic_mbps": "traffic_mbps",
            "loss_percent": "loss_percent",
        }
    )

    @model_validator(mode="after")
    def _consistent(self) -> SourceConfig:
        if self.type == "csv" and not self.path:
            raise ValueError("source.type=csv exige source.path")
        if self.type == "sql":
            if self.url is None:
                raise ValueError("source.type=sql exige source.url")
            for ident in (self.table, *self.columns.values()):
                if not _SQL_IDENTIFIER.match(ident):
                    raise ValueError(f"identifiant SQL invalide : {ident!r}")
        return self


class AppConfig(BaseModel):
    """Racine de la configuration (§35)."""

    source: SourceConfig = SourceConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    history: HistoryConfig = HistoryConfig()
    model_selection: ModelSelectionConfig = ModelSelectionConfig()
    recalibration: RecalibrationConfig = RecalibrationConfig()
    forecast: ForecastConfig = ForecastConfig()
    detection: DetectionConfig = DetectionConfig()
    incident: IncidentConfig = IncidentConfig()
    alerts: AlertsConfig = AlertsConfig()

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """
        Charge la configuration depuis un fichier YAML avec `safe_load`
        (jamais `yaml.load`, pour ne pas permettre l'exécution de code
        arbitraire via des tags YAML personnalisés).
        """
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    @classmethod
    def defaults(cls) -> AppConfig:
        return cls()
