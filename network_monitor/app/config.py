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
import string
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from network_monitor.app.models import Severity

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


class PrometheusOutput(BaseModel):
    traffic: str = Field(min_length=1)
    """Expression PromQL du trafic **en Mbps** (ex. `rate(ifHCInOctets{...}[5m]) * 8 / 1e6`)."""
    loss: str | None = None
    """Expression PromQL du taux de perte **en %**. Sans elle, la perte est supposée nulle (la
    détection sur les pertes n'a alors rien à observer)."""


class SourceConfig(BaseModel):
    """Source de données (§6) : Mock (défaut), rejeu CSV, ou table SQL."""

    type: Literal["mock", "csv", "parquet", "sql", "prometheus"] = "mock"
    path: str | None = None
    url: SecretStr | None = None
    """URL SQLAlchemy (peut contenir un mot de passe) : jamais renvoyée en
    clair par GET /config."""
    prometheus: dict[str, PrometheusOutput] = Field(default_factory=dict)
    """Une entrée par sortie : l'identifiant de la sortie et ses requêtes PromQL."""
    bearer_token_env: str | None = None
    """Nom de la variable d'environnement qui porte un jeton porteur pour l'API Prometheus
    (le jeton lui-même n'est jamais dans la configuration)."""
    lag: timedelta = timedelta(seconds=30)
    """Retard laissé aux collecteurs : on n'interroge que jusqu'à `maintenant - lag`."""
    table: str = "traffic_measurements"
    columns: dict[str, str] = Field(
        default_factory=lambda: {
            "timestamp": "timestamp",
            "output_id": "output_id",
            "traffic_mbps": "traffic_mbps",
            "loss_percent": "loss_percent",
        }
    )

    @field_validator("lag", mode="before")
    @classmethod
    def _parse_lag(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v

    @model_validator(mode="after")
    def _consistent(self) -> SourceConfig:
        if self.type in ("csv", "parquet") and not self.path:
            raise ValueError(f"source.type={self.type} exige source.path")
        if self.type == "prometheus" and (self.url is None or not self.prometheus):
            raise ValueError("source.type=prometheus exige source.url et au moins une sortie dans source.prometheus")
        if self.type == "sql":
            if self.url is None:
                raise ValueError("source.type=sql exige source.url")
            for ident in (self.table, *self.columns.values()):
                if not _SQL_IDENTIFIER.match(ident):
                    raise ValueError(f"identifiant SQL invalide : {ident!r}")
        return self


_ALERT_EVENTS = frozenset({"opened", "updated", "reopened", "closed", "stale", "escalated"})


TEMPLATE_FIELDS = (
    "output", "event", "type", "severity", "start", "end", "at", "duration", "traffic", "expected", "deviation", "loss", "impacted", "message",
)
"""Champs qu'un modèle de message peut insérer, déjà mis en forme (voir `notify._fields`) ; « ? » quand la valeur manque."""
_MAX_TEMPLATE_LENGTH = 600


def _check_template(template: str) -> str:
    """Un modèle est du texte avec des `{champ}` simples. Rien d'autre : ni attribut (`{output.x}`), ni indice, ni conversion, ni format,
    qui permettraient d'atteindre autre chose que les champs prévus."""
    if len(template) > _MAX_TEMPLATE_LENGTH:
        raise ValueError(f"modèle trop long ({len(template)} > {_MAX_TEMPLATE_LENGTH} caractères)")
    for _, field, spec, conversion in string.Formatter().parse(template):  # lève ValueError sur des accolades mal fermées
        if field is None:
            continue
        if field not in TEMPLATE_FIELDS or spec or conversion:
            raise ValueError(f"champ « {{{field}{':' + spec if spec else ''}{'!' + conversion if conversion else ''}}} » non autorisé (champs valides : {', '.join(TEMPLATE_FIELDS)})")
    return template


class _ChannelBase(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    min_severity: Severity = Severity.WARNING
    """Sévérité minimale d'une alerte pour être envoyée sur ce canal."""
    events: list[str] = Field(default_factory=lambda: ["opened", "updated", "reopened", "closed", "stale"])
    """Événements notifiés (`suspect` et `recovering` ne le sont jamais : trop bavards)."""
    max_messages_per_minute: int | None = Field(default=None, ge=1)
    max_messages_per_hour: int | None = Field(default=None, ge=1)
    """Plafond de messages (pas d'alertes) : au-delà, les alertes s'accumulent et partent
    regroupées dans un seul message dès que la fenêtre se libère — rien n'est perdu, mais le
    coût (SMS) et le bruit sont bornés."""

    language: Literal["fr", "en"] = "fr"
    """Langue des messages de ce canal (le groupe d'astreinte peut recevoir de l'anglais, le NOC du français)."""
    templates: dict[str, str] = Field(default_factory=dict)
    """Modèles de message par événement (`{"opened": "{output} : {type} depuis {start}"}`) ; sans modèle, le message intégré s'applique.
    Champs : voir `TEMPLATE_FIELDS`. Pour un SMS, le texte est de toute façon dépouillé de ses accents et coupé à 160 caractères."""

    @field_validator("templates")
    @classmethod
    def _valid_templates(cls, v: dict[str, str]) -> dict[str, str]:
        unknown = set(v) - _ALERT_EVENTS
        if unknown:
            raise ValueError(f"événements inconnus dans `templates` : {sorted(unknown)} (valides : {sorted(_ALERT_EVENTS)})")
        for event, template in v.items():
            try:
                _check_template(template)
            except ValueError as exc:
                raise ValueError(f"modèle de « {event} » : {exc}") from None
        return v

    @field_validator("events")
    @classmethod
    def _known_events(cls, v: list[str]) -> list[str]:
        unknown = set(v) - _ALERT_EVENTS
        if unknown:
            raise ValueError(f"événements inconnus : {sorted(unknown)} (valides : {sorted(_ALERT_EVENTS)})")
        return v


class TelegramChannel(_ChannelBase):
    """Groupe Telegram : un bot ajouté au groupe, identifiant du groupe (négatif, ex. -1001234567890)."""

    type: Literal["telegram"] = "telegram"
    chat_id: str = Field(min_length=1)
    token_env: str = "NM_TELEGRAM_TOKEN"
    max_messages_per_minute: int | None = Field(default=15, ge=1)  # Telegram : ~20 par minute et par groupe


class WebhookChannel(_ChannelBase):
    """POST JSON signé (HMAC-SHA256) vers n'importe quel système (Slack/Teams via passerelle, ticketing, scripts...)."""

    type: Literal["webhook"] = "webhook"
    url_env: str = "NM_WEBHOOK_URL"
    secret_env: str | None = "NM_WEBHOOK_SECRET"


class SmsChannel(_ChannelBase):
    """SMS à un groupe de numéros. `android_gateway` : un téléphone Android avec une SIM locale
    (application SMS Gateway, gratuit hors forfait de la SIM). `orange` : API Orange Developer
    (forfait SMS à acheter)."""

    type: Literal["sms"] = "sms"
    provider: Literal["android_gateway", "orange"] = "android_gateway"
    recipients: list[str] = Field(min_length=1)
    base_url_env: str = "NM_SMS_URL"
    """android_gateway : adresse du téléphone (ex. http://192.168.1.50:8080)."""
    username_env: str = "NM_SMS_USER"
    password_env: str = "NM_SMS_PASSWORD"
    client_id_env: str = "NM_ORANGE_CLIENT_ID"
    client_secret_env: str = "NM_ORANGE_CLIENT_SECRET"
    sender: str | None = None
    """orange : numéro expéditeur autorisé, au format international (ex. +2370000)."""
    min_severity: Severity = Severity.CRITICAL
    events: list[str] = Field(default_factory=lambda: ["opened", "closed"])
    max_messages_per_hour: int | None = Field(default=20, ge=1)

    @field_validator("recipients")
    @classmethod
    def _e164(cls, v: list[str]) -> list[str]:
        bad = [n for n in v if not re.fullmatch(r"\+[1-9][0-9]{7,14}", n)]
        if bad:
            raise ValueError(f"numéros au format international attendus (+237...) : {bad}")
        return v

    @model_validator(mode="after")
    def _provider_needs(self) -> SmsChannel:
        if self.provider == "orange" and not self.sender:
            raise ValueError("sms/orange exige `sender`")
        return self


Channel = Annotated[TelegramChannel | WebhookChannel | SmsChannel, Field(discriminator="type")]


class NotificationsConfig(BaseModel):
    """Alertes sortantes (§27). Les secrets (jetons, mots de passe, URL) ne sont jamais ici :
    chaque canal nomme la variable d'environnement qui les porte."""

    enabled: bool = False
    channels: list[Channel] = Field(default_factory=list)
    batch_window: timedelta = timedelta(seconds=20)
    """Les alertes qui arrivent à moins de `batch_window` l'une de l'autre partent dans un seul message."""
    max_attempts: int = Field(default=6, ge=1, le=20)
    retry_base: timedelta = timedelta(seconds=30)
    """Délai avant la 2e tentative ; double à chaque échec, plafonné à 15 min."""
    timezone: str = "UTC"
    """Fuseau d'affichage des heures dans les messages (ex. Africa/Douala)."""
    escalate_after: timedelta | None = None
    """Un incident confirmé, en cours, que personne n'a pris en charge (`PUT /incidents/{id}/ack`) depuis ce délai émet UNE
    alerte `escalated`, envoyée aux canaux dont `events` la contient (le « second groupe »). `None` : pas d'escalade."""

    @field_validator("batch_window", "retry_base", "escalate_after", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if v is not None and not isinstance(v, timedelta) else v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"fuseau inconnu : {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _unique_names(self) -> NotificationsConfig:
        names = [c.name for c in self.channels]
        if len(names) != len(set(names)):
            raise ValueError("deux canaux portent le même nom")
        if self.enabled and not self.channels:
            raise ValueError("notifications.enabled exige au moins un canal")
        if self.escalate_after is not None:
            if self.escalate_after <= timedelta(0):
                raise ValueError("notifications.escalate_after doit être positif")
            if not self.enabled or not any("escalated" in c.events for c in self.channels):
                raise ValueError(
                    "notifications.escalate_after exige des notifications actives et un canal dont `events` contient « escalated » : "
                    "sans lui, personne ne recevrait l'escalade"
                )
        return self


class TopologyConfig(BaseModel):
    """Dépendances entre sorties : la clé dépend de la valeur (« la sortie aval a pour parent la sortie amont »)."""

    parents: dict[str, str] = Field(default_factory=dict)
    """Ex. `{LINK_DOUALA_BAFOUSSAM: LINK_DOUALA_YAOUNDE}` : une panne de la seconde explique celle de la première."""
    correlation_window: timedelta = timedelta(minutes=15)
    """Deux incidents de sorties dépendantes dont les débuts réels sont distants de moins que cela sont
    rattachés à la même cause : l'aval ne notifie pas, l'amont dit qui d'autre il touche."""

    @field_validator("correlation_window", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v

    @model_validator(mode="after")
    def _acyclic(self) -> TopologyConfig:
        for child, parent in self.parents.items():
            if child == parent:
                raise ValueError(f"{child} ne peut pas être son propre parent")
            seen, node = {child}, parent
            while node in self.parents:
                if node in seen:
                    raise ValueError(f"cycle dans la topologie autour de {child}")
                seen.add(node)
                node = self.parents[node]
        return self


class HeartbeatConfig(BaseModel):
    """Signal de vie externe (interrupteur d'homme mort) : un service tiers (ex. Healthchecks.io, gratuit et sans
    carte) alerte quand les appels cessent, ce qu'aucune autosurveillance interne ne peut faire si le processus meurt.
    L'URL (secret) n'est jamais ici : `url_env` nomme la variable d'environnement qui la porte."""

    enabled: bool = False
    url_env: str = "NM_HEARTBEAT_URL"
    period: timedelta = timedelta(seconds=60)
    timeout: timedelta = timedelta(seconds=10)
    fail_suffix: str = "/fail"
    """Ajouté à l'URL pour signaler explicitement un défaut (les raisons sont envoyées en corps)."""

    @field_validator("period", "timeout", mode="before")
    @classmethod
    def _parse(cls, v):
        return _parse_duration(v) if not isinstance(v, timedelta) else v


class StorageConfig(BaseModel):
    database_url: str = "sqlite:///network_monitor.db"
    lock_file: str | None = None
    """Verrou d'instance unique (voir app/lock.py). Par défaut : à côté de la base SQLite, sinon
    `network_monitor.lock` dans le dossier courant."""
    archive_dir: str | None = None
    """Dossier d'archive Parquet : les mesures brutes retirées par la purge y sont rangées
    (un dossier par sortie, un fichier par jour) au lieu d'être perdues."""


class AppConfig(BaseModel):
    """Racine de la configuration (§35)."""

    source: SourceConfig = SourceConfig()
    storage: StorageConfig = StorageConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    history: HistoryConfig = HistoryConfig()
    model_selection: ModelSelectionConfig = ModelSelectionConfig()
    recalibration: RecalibrationConfig = RecalibrationConfig()
    forecast: ForecastConfig = ForecastConfig()
    detection: DetectionConfig = DetectionConfig()
    incident: IncidentConfig = IncidentConfig()
    alerts: AlertsConfig = AlertsConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    topology: TopologyConfig = TopologyConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()

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
