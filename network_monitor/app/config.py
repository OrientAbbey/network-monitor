"""
Configuration du système (§35). Chargée via `yaml.safe_load` uniquement
(jamais `yaml.load`), pour empêcher l'exécution de code arbitraire depuis
un fichier de configuration non fiable.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


def _parse_duration(value: str | int | float) -> timedelta:
    """
    Convertit '5m', '30d', '10min'… (ou un nombre de secondes) en timedelta.
    Une durée négative est refusée : une `minimum_duration` négative contournerait l'hystérésis (§21).
    """
    if isinstance(value, bool):  # bool est un int en Python
        raise ValueError(f"Durée non reconnue : {value!r}")
    if isinstance(value, (int, float)):
        result = timedelta(seconds=float(value))
    else:
        text = str(value).strip().lower()
        if text in {"0", "0.0"}:
            result = timedelta(0)
        else:
            units = {"s": "seconds", "m": "minutes", "min": "minutes", "h": "hours", "d": "days"}
            for suffix, unit in sorted(units.items(), key=lambda kv: -len(kv[0])):
                if text.endswith(suffix):
                    result = timedelta(**{unit: float(text[: -len(suffix)])})
                    break
            else:
                raise ValueError(f"Durée non reconnue : {value!r}")
    if result < timedelta(0):
        raise ValueError(f"Durée négative refusée : {value!r}")
    return result


def _coerce_duration(v):
    """Point d'entrée des validateurs : accepte aussi un `timedelta` déjà construit, et refuse les négatifs."""
    if isinstance(v, timedelta):
        if v < timedelta(0):
            raise ValueError(f"Durée négative refusée : {v!r}")
        return v
    return _parse_duration(v)


class ModelSelectionConfig(BaseModel):
    minimum_history: timedelta = timedelta(days=45)
    validation_window: timedelta = timedelta(days=7)

    @field_validator("minimum_history", "validation_window", mode="before")
    @classmethod
    def _parse(cls, v):
        return _coerce_duration(v)


class LossDetectionConfig(BaseModel):
    enabled: bool = True
    loss_std_floor: float = Field(default=0.05, ge=0.0)


class TrafficDetectionConfig(BaseModel):
    enabled: bool = True


class _NotImplementedFlag(BaseModel):
    """
    Option prévue par le cahier des charges (§9.2, §17, §36) mais pas encore
    implémentée. Un drapeau qui ne ferait rien quand on l'active serait
    trompeur : on refuse explicitement `enabled: true` plutôt que de
    l'ignorer en silence.
    """

    enabled: bool = False

    @field_validator("enabled")
    @classmethod
    def _must_stay_disabled(cls, v):
        if v:
            raise ValueError("option non implémentée dans cette version (voir §36 : évolution prévue)")
        return v


class DetectionConfig(BaseModel):
    traffic: TrafficDetectionConfig = TrafficDetectionConfig()
    loss: LossDetectionConfig = LossDetectionConfig()
    isolation_forest: _NotImplementedFlag = _NotImplementedFlag()
    extended_features: _NotImplementedFlag = _NotImplementedFlag()


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
    minimum_duration: timedelta = timedelta(minutes=10)
    recovery_duration: timedelta = timedelta(minutes=10)
    stale_after: timedelta = timedelta(minutes=15)
    per_output_overrides: dict[str, IncidentOutputOverride] = Field(default_factory=dict)

    @field_validator("minimum_duration", "recovery_duration", "stale_after", mode="before")
    @classmethod
    def _parse(cls, v):
        return _coerce_duration(v)

    @model_validator(mode="after")
    def _thresholds_are_ordered(self):
        # §21. Un warning_score au-dessus de open_score laisserait une plage de scores sans aucun effet.
        if not (self.close_score < self.warning_score < self.open_score):
            raise ValueError(
                "les seuils doivent respecter close_score < warning_score < open_score (§21) : "
                f"reçu close={self.close_score}, warning={self.warning_score}, open={self.open_score}"
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
    """
    `enabled` coupe la génération des alertes. Le seuil d'entrée en SUSPECT
    (alerte INFO) est `incident.warning_score` (§35 précise que les deux
    doivent être alignés) : il n'existe donc pas de second seuil ici — une
    clé `alerts.warning_score` présente dans un ancien YAML est ignorée.
    """

    enabled: bool = True
    critical_score: float = Field(default=0.80, ge=0.0, le=1.0)


class ForecastConfig(BaseModel):
    horizon: timedelta = timedelta(minutes=30)
    models: list[str] = Field(default_factory=lambda: ["seasonal_baseline", "holt_winters"])

    @field_validator("models")
    @classmethod
    def _known_models(cls, v):
        known = {"seasonal_baseline", "holt_winters"}
        unknown = set(v) - known
        if unknown:
            raise ValueError(f"modèle(s) inconnu(s) : {sorted(unknown)} (connus : {sorted(known)})")
        if not v:
            raise ValueError("au moins un modèle de prévision est requis")
        return v

    @field_validator("horizon", mode="before")
    @classmethod
    def _parse(cls, v):
        return _coerce_duration(v)


class HistoryConfig(BaseModel):
    minimum: timedelta = timedelta(days=30)
    preferred: timedelta = timedelta(days=90)

    @field_validator("minimum", "preferred", mode="before")
    @classmethod
    def _parse(cls, v):
        return _coerce_duration(v)


class MonitoringConfig(BaseModel):
    interval: timedelta = timedelta(minutes=5)
    recalibration_interval: timedelta | None = timedelta(hours=24)
    """Fréquence de recalibration des modèles (§32) ; `None` la désactive. Les
    périodes couvertes par un incident confirmé sont exclues des données de
    recalibration pour ne pas apprendre l'anomalie comme un comportement normal."""

    @field_validator("interval", mode="before")
    @classmethod
    def _parse(cls, v):
        return _coerce_duration(v)

    @field_validator("recalibration_interval", mode="before")
    @classmethod
    def _parse_recalibration(cls, v):
        if v is None:
            return None
        result = _coerce_duration(v)
        if result == timedelta(0):
            raise ValueError("recalibration_interval doit être > 0 (utiliser null pour désactiver)")
        return result


class StorageConfig(BaseModel):
    retention: timedelta | None = timedelta(days=30)
    """Durée de conservation des mesures, prévisions et anomalies ; `None` = illimitée.
    Les incidents et alertes ne sont jamais purgés."""
    purge_interval: timedelta = timedelta(days=1)

    @field_validator("retention", mode="before")
    @classmethod
    def _parse_retention(cls, v):
        if v is None:
            return None
        result = _coerce_duration(v)
        if result == timedelta(0):
            raise ValueError("retention doit être > 0 (utiliser null pour une conservation illimitée)")
        return result

    @field_validator("purge_interval", mode="before")
    @classmethod
    def _parse_purge(cls, v):
        result = _coerce_duration(v)
        if result == timedelta(0):
            raise ValueError("purge_interval doit être > 0")
        return result


class AppConfig(BaseModel):
    """Racine de la configuration (§35)."""

    monitoring: MonitoringConfig = MonitoringConfig()
    storage: StorageConfig = StorageConfig()
    history: HistoryConfig = HistoryConfig()
    model_selection: ModelSelectionConfig = ModelSelectionConfig()
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
