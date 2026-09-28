"""
Configuration du système (§35). Chargée via `yaml.safe_load` uniquement
(jamais `yaml.load`), pour empêcher l'exécution de code arbitraire depuis
un fichier de configuration non fiable.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


def _parse_duration(value: str | int | float) -> timedelta:
    """Convertit '5m', '30d', '10min' etc. en timedelta. Accepte aussi un nombre de secondes."""
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    text = str(value).strip().lower()
    units = {"s": "seconds", "m": "minutes", "min": "minutes", "h": "hours", "d": "days"}
    for suffix, unit in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            number = text[: -len(suffix)]
            return timedelta(**{unit: float(number)})
    raise ValueError(f"Durée non reconnue : {value!r}")


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


class _EnabledFlag(BaseModel):
    enabled: bool = False


class DetectionConfig(BaseModel):
    traffic: _EnabledFlag = _EnabledFlag(enabled=True)
    loss: LossDetectionConfig = LossDetectionConfig()
    isolation_forest: _EnabledFlag = _EnabledFlag(enabled=False)
    extended_features: _EnabledFlag = _EnabledFlag(enabled=False)


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
        return _parse_duration(v) if not isinstance(v, timedelta) else v

    @field_validator("close_score")
    @classmethod
    def _close_below_open(cls, v, info):
        open_score = info.data.get("open_score")
        if open_score is not None and v >= open_score:
            raise ValueError("close_score doit être strictement inférieur à open_score (§21)")
        return v

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
    warning_score: float = Field(default=0.50, ge=0.0, le=1.0)
    critical_score: float = Field(default=0.80, ge=0.0, le=1.0)


class ForecastConfig(BaseModel):
    horizon: timedelta = timedelta(minutes=30)
    models: list[str] = Field(default_factory=lambda: ["seasonal_baseline", "holt_winters"])

    @field_validator("horizon", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v


class HistoryConfig(BaseModel):
    minimum: timedelta = timedelta(days=30)
    preferred: timedelta = timedelta(days=90)

    @field_validator("minimum", "preferred", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v


class MonitoringConfig(BaseModel):
    interval: timedelta = timedelta(minutes=5)

    @field_validator("interval", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v


class AppConfig(BaseModel):
    """Racine de la configuration (§35)."""

    monitoring: MonitoringConfig = MonitoringConfig()
    history: HistoryConfig = HistoryConfig()
    model_selection: ModelSelectionConfig = ModelSelectionConfig()
    forecast: ForecastConfig = ForecastConfig()
    detection: DetectionConfig = DetectionConfig()
    incident: IncidentConfig = IncidentConfig()
    alerts: AlertsConfig = AlertsConfig()

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AppConfig":
        """
        Charge la configuration depuis un fichier YAML avec `safe_load`
        (jamais `yaml.load`, pour ne pas permettre l'exécution de code
        arbitraire via des tags YAML personnalisés).
        """
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)

    @classmethod
    def defaults(cls) -> "AppConfig":
        return cls()
