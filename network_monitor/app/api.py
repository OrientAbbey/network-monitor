"""
API FastAPI (§37) : expose le système de monitoring en HTTP.

Couche fine au-dessus de MonitoringLoop / Repository / IncidentManager —
aucune logique métier propre ici, uniquement sérialisation et orchestration
du cycle de vie (démarrage/arrêt de la boucle en tâche de fond).

Sécurité : les endpoints qui modifient l'état (PUT /config, POST
/monitoring/start|stop) exigent un jeton porteur (`NM_API_TOKEN`) — voir
`require_token`. Sans ce jeton configuré côté serveur, ces endpoints
refusent tout appel (fail closed) plutôt que de rester ouverts par défaut.
Le CORS est restreint à `NM_CORS_ORIGINS` (origine du frontend de
développement par défaut), pas à `*`.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, SecretStr

from network_monitor.app import auth
from network_monitor.app.config import AppConfig, HistoryConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import (
    AnomalyResult,
    ForecastResult,
    Incident,
    TrafficSample,
)
from network_monitor.data.sources import CsvDataSource, build_source
from network_monitor.incidents.manager import Alert
from network_monitor.storage.repository import Repository, SqlAlchemyRepository

logger = logging.getLogger(__name__)

# Configuration par défaut de l'API — distincte de AppConfig.defaults().
# Avec l'historique "préféré" du cahier des charges (90 jours, au-delà du
# seuil de 45 jours déclenchant la sélection de modèle), la calibration
# ajuste Holt-Winters sur chaque sortie : plusieurs secondes, parfois sans
# converger (cf. avertissements statsmodels rencontrés en développement).
# Cela bloquerait le démarrage de l'API (le lifespan attend la calibration
# avant de répondre aux requêtes). En contexte de démonstration/développement
# avec source Mock, un historique plus court reste sous le seuil et retombe
# sur la Seasonal Baseline, nettement plus rapide (§11). Un déploiement réel
# peut toujours passer AppConfig.defaults() explicitement à MonitoringState.
#
# Ne jamais muter cet objet en place : chaque MonitoringState en prend une
# copie (voir __init__) pour qu'un PUT /config sur une instance ne modifie
# jamais ce gabarit partagé par toutes les instances du process (tests inclus).
DEFAULT_API_CONFIG = AppConfig(history=HistoryConfig(minimum=timedelta(days=30), preferred=timedelta(days=40)))

_MAX_WINDOW_SPAN = timedelta(days=31)
"""Plage maximale pour un `since`/`until` explicite (§3) : évite qu'une
requête mal bornée ne tente de sérialiser des mois de mesures en une fois."""


def require_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    """
    Dépendance FastAPI protégeant les endpoints de contrôle (§37). Renvoie l'identité
    authentifiée (nom d'utilisateur, ou "api-token" pour le jeton machine), journalisée
    par les endpoints qui modifient l'état.

    `fail closed` : sans utilisateur ni `NM_API_TOKEN` configuré, ces endpoints refusent
    tout appel plutôt que de rester ouverts — un déploiement qui « oublie » de configurer
    l'authentification reste protégé, juste inutilisable tant qu'elle ne l'est pas.
    """
    settings = auth.load_settings()
    if not settings.configured:
        raise HTTPException(
            status_code=503,
            detail="Authentification non configurée côté serveur (NM_USERS ou NM_API_TOKEN) : endpoint désactivé",
        )
    bearer = (authorization or "").removeprefix("Bearer ").strip()
    subject = auth.subject_for(settings, bearer) if bearer else None
    if subject is None:
        logger.warning("Accès refusé à %s depuis %s", request.url.path, _client(request))
        raise HTTPException(status_code=401, detail="Jeton d'authentification invalide, expiré ou absent")
    return subject


def _client(request: Request) -> str:
    # Derrière un proxy, c'est l'adresse du proxy : le limiteur de connexion est alors global.
    return request.client.host if request.client else "inconnu"


# ---------------------------------------------------------------------------
# État du service : configuration, boucle, thread d'exécution
# ---------------------------------------------------------------------------


class MonitoringState:
    """
    Regroupe tout ce que l'API doit garder en mémoire entre les requêtes.
    La boucle de monitoring tourne dans un thread dédié : `run_forever`
    (app/main.py) est bloquant et n'a pas de notion de rythme réel, donc
    l'API pilote elle-même l'avancée du temps simulé, à un rythme accéléré
    (`seconds_per_tick`) adapté à une démonstration plutôt qu'aux 5 minutes
    réelles qu'implique §3 du cahier des charges.
    """

    def __init__(self, config: AppConfig | None = None, db_url: str = "sqlite:///network_monitor.db") -> None:
        # Copie profonde : cette instance ne doit jamais pouvoir muter
        # DEFAULT_API_CONFIG (ni la config passée par l'appelant), qu'elle
        # vienne d'ici ou d'un futur PUT /config (§35).
        self.config = (config or DEFAULT_API_CONFIG).model_copy(deep=True)
        self.storage: Repository = SqlAlchemyRepository(db_url)
        self.source = build_source(self.config)
        self.live = bool(getattr(self.source, "live", False))
        self.loop = MonitoringLoop(self.source, self.storage, self.config)
        interval = self.config.monitoring.interval
        # Source « live » (SQL) : horloge réelle, un tick par intervalle. Rejeu CSV :
        # horloge simulée qui démarre à la bascule historique → rejeu du fichier.
        # Mock : horloge simulée accélérée, calée sur l'heure actuelle.
        if isinstance(self.source, CsvDataSource):
            self.now = self.source.default_start()
        else:
            self.now = _aligned_now(interval)
        self.seconds_per_tick = interval.total_seconds() if self.live else 2.0
        self.calibrated = False
        self.running = False
        self._clock_started = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def calibrate(self) -> None:
        self.loop.calibrate(self.now)
        self.calibrated = True

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            if not self.calibrated:
                self.calibrate()
            if not self._clock_started:
                if hasattr(self.source, "start_realtime"):
                    self.source.start_realtime(self.now)
                self._clock_started = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            self.running = True

    def stop(self) -> None:
        with self._lock:
            if not self.running:
                return
            self._stop.set()
            thread, self._thread = self._thread, None
            self.running = False
        # `join()` hors du verrou : `_run` ne le prend jamais, mais attendre
        # sous verrou bloquerait inutilement un `start()` concurrent.
        if thread is not None:
            thread.join(timeout=5.0)

    def _run(self) -> None:
        interval = self.source.get_interval()
        while not self._stop.wait(self.seconds_per_tick):
            with self._lock:
                self.now = _aligned_now(interval) if self.live else self.now + interval
                now = self.now
            try:
                self.loop.run_tick(now)
            except Exception:  # ne jamais laisser une erreur ponctuelle tuer la boucle
                logger.exception("Erreur pendant run_tick à %s", now)


def _aligned_now(interval: timedelta) -> datetime:
    """Arrondit l'heure de départ de l'horloge simulée à la grille de
    `interval`, pour produire des horodatages ronds plutôt que décalés de
    quelques secondes (ex. 19:30:59.37) au fil des redémarrages."""
    now = datetime.now(timezone.utc)
    seconds = interval.total_seconds()
    epoch = now.timestamp()
    aligned = (epoch // seconds) * seconds
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def _initial_config() -> AppConfig:
    """`NM_CONFIG` pointe vers un YAML (voir config.example.yaml) ; sinon la
    configuration de démonstration ci-dessus."""
    path = os.environ.get("NM_CONFIG")
    return AppConfig.from_yaml(path) if path else DEFAULT_API_CONFIG


state = MonitoringState(_initial_config())


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.calibrate()
    state.start()
    yield
    state.stop()


_throttle = auth.LoginThrottle()
_is_development = os.environ.get("NM_ENV", "development") == "development"

app = FastAPI(
    title="Network Monitor API",
    description="Détection d'anomalies réseau non supervisée — voir le cahier des charges du projet.",
    version="1.0.0",
    lifespan=lifespan,
    # /docs et /openapi.json exposent la structure complète de l'API : utile
    # en développement, à ne pas laisser accessible sans réflexion ailleurs
    # (S-5). NM_ENV=production (ou toute valeur ≠ "development") les masque.
    docs_url="/docs" if _is_development else None,
    redoc_url="/redoc" if _is_development else None,
    openapi_url="/openapi.json" if _is_development else None,
)

_default_origins = "http://localhost:5173,http://127.0.0.1:5173"
_cors_origins = [o.strip() for o in os.environ.get("NM_CORS_ORIGINS", _default_origins).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---------------------------------------------------------------------------
# Schémas de réponse spécifiques à l'API (au-delà des modèles du cœur métier)
# ---------------------------------------------------------------------------


class OutputSummary(BaseModel):
    output_id: str
    latest_sample: TrafficSample | None = None
    status: str = "normal"  # "normal" si aucun incident actif
    active_incident_id: str | None = None


class MonitoringStatus(BaseModel):
    running: bool
    calibrated: bool
    simulated_now: datetime
    server_time: datetime
    """Heure réelle du serveur (UTC) — permet au client de mesurer l'écart
    avec `simulated_now` au lieu de comparer `simulated_now` à sa propre
    horloge locale, qui peut être en avance ou en retard (§13)."""
    interval_seconds: float
    seconds_per_tick: float
    outputs: list[str]


class ConfigUpdate(BaseModel):
    """Sous-ensemble modifiable à chaud de la configuration (§35, §21)."""

    open_score: float | None = Field(default=None, ge=0.0, le=1.0)
    close_score: float | None = Field(default=None, ge=0.0, le=1.0)
    warning_score: float | None = Field(default=None, ge=0.0, le=1.0)
    minimum_duration_seconds: float | None = Field(default=None, gt=0)
    recovery_duration_seconds: float | None = Field(default=None, gt=0)
    critical_score: float | None = Field(default=None, ge=0.0, le=1.0)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    username: str


class AlertOut(BaseModel):
    """Représentation API d'une alerte (§27/§28)."""

    incident_id: str
    output_id: str
    severity: str
    event: str
    timestamp: datetime
    message: str
    incident_type: str
    duration_seconds: float | None = None
    traffic_mbps: float | None = None
    expected_traffic_mbps: float | None = None
    traffic_deviation_percent: float | None = None
    loss_percent: float | None = None
    expected_loss_percent: float | None = None

    @classmethod
    def from_alert(cls, alert: Alert) -> AlertOut:
        return cls(
            incident_id=alert.incident_id,
            output_id=alert.output_id,
            severity=alert.severity.value,
            event=alert.event,
            timestamp=alert.timestamp,
            message=alert.message,
            incident_type=alert.incident_type.value,
            duration_seconds=alert.duration_seconds,
            traffic_mbps=alert.traffic_mbps,
            expected_traffic_mbps=alert.expected_traffic_mbps,
            traffic_deviation_percent=alert.traffic_deviation_percent,
            loss_percent=alert.loss_percent,
            expected_loss_percent=alert.expected_loss_percent,
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/outputs", response_model=list[OutputSummary])
def list_outputs() -> list[OutputSummary]:
    interval = state.source.get_interval()
    active = {i.output_id: i for i in state.storage.list_active_incidents()}
    summaries = []
    for output_id in state.source.list_outputs():
        history = state.storage.get_history(output_id, state.now - interval * 3, state.now + interval)
        incident = active.get(output_id)
        summaries.append(
            OutputSummary(
                output_id=output_id,
                latest_sample=history[-1] if history else None,
                status=incident.status.value if incident else "normal",
                active_incident_id=incident.id if incident else None,
            )
        )
    return summaries


def _parse_window(
    since: datetime | None, until: datetime | None, default_span: timedelta
) -> tuple[datetime, datetime]:
    for label, value in (("since", since), ("until", until)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                status_code=422,
                detail=f"`{label}` doit inclure un fuseau horaire (ex. suffixe Z ou +00:00), §3",
            )
    end = until or state.now
    start = since or (end - default_span)
    if start >= end:
        raise HTTPException(status_code=400, detail="`since` doit être antérieur à `until`")
    if end - start > _MAX_WINDOW_SPAN:
        raise HTTPException(
            status_code=400,
            detail=f"la plage demandée dépasse le maximum autorisé de {_MAX_WINDOW_SPAN.days} jours",
        )
    return start, end


def _ensure_known_output(output_id: str) -> None:
    if output_id not in state.source.list_outputs():
        raise HTTPException(status_code=404, detail=f"Sortie inconnue : {output_id}")


@app.get("/outputs/{output_id}/history", response_model=list[TrafficSample])
def output_history(
    output_id: str,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    span_hours: Annotated[float | None, Query(gt=0, le=_MAX_WINDOW_SPAN.days * 24)] = None,
) -> list[TrafficSample]:
    """`span_hours` demande une fenêtre relative à l'heure *serveur*
    (`state.now`), pas à l'heure du client : c'est ce que le dashboard doit
    utiliser plutôt que de reconstruire `since`/`until` depuis `new Date()`
    (§13 — l'horloge simulée avance à un rythme différent de l'horloge réelle)."""
    _ensure_known_output(output_id)
    if span_hours is not None and since is None and until is None:
        since = state.now - timedelta(hours=span_hours)
        until = state.now
    start, end = _parse_window(since, until, timedelta(hours=12))
    return state.storage.get_history(output_id, start, end)


@app.get("/outputs/{output_id}/anomalies", response_model=list[AnomalyResult])
def output_anomalies(
    output_id: str,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    span_hours: Annotated[float | None, Query(gt=0, le=_MAX_WINDOW_SPAN.days * 24)] = None,
) -> list[AnomalyResult]:
    _ensure_known_output(output_id)
    if span_hours is not None and since is None and until is None:
        since = state.now - timedelta(hours=span_hours)
        until = state.now
    start, end = _parse_window(since, until, timedelta(hours=12))
    return state.storage.get_anomalies(output_id, start, end)


@app.get("/outputs/{output_id}/forecast", response_model=list[ForecastResult])
def output_forecast(
    output_id: str,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    span_hours: Annotated[float | None, Query(gt=0, le=_MAX_WINDOW_SPAN.days * 24)] = None,
) -> list[ForecastResult]:
    _ensure_known_output(output_id)
    if span_hours is not None and since is None and until is None:
        since = state.now - timedelta(hours=span_hours)
        until = state.now
    start, end = _parse_window(since, until, timedelta(hours=12))
    return state.storage.get_forecasts(output_id, start, end)


@app.get("/outputs/{output_id}/forecast/ahead", response_model=list[ForecastResult])
def output_forecast_ahead(
    output_id: str,
    horizon_minutes: Annotated[int, Query(ge=5, le=24 * 60)] = 35,
) -> list[ForecastResult]:
    """
    Prévision à la demande au-delà de l'instant simulé courant (§13) —
    alimente la courbe "Prédiction" du dashboard, calculée à la volée à
    partir du modèle déjà calibré, sans rien persister.
    """
    _ensure_known_output(output_id)
    try:
        return state.loop.forecast_ahead(output_id, state.now, timedelta(minutes=horizon_minutes))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/incidents", response_model=list[Incident])
def list_incidents(
    status: Annotated[list[str] | None, Query(description="Filtrer par statut : suspect, open, stale, closed")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[Incident]:
    return state.storage.list_incidents(statuses=status, limit=limit)


@app.get("/incidents/{incident_id}", response_model=Incident)
def get_incident(incident_id: str) -> Incident:
    incident = state.storage.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"Incident inconnu : {incident_id}")
    return incident


@app.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    output_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AlertOut]:
    """Historique des alertes émises (§27), les plus récentes d'abord."""
    if output_id is not None:
        _ensure_known_output(output_id)
    return [AlertOut.from_alert(a) for a in state.storage.list_alerts(output_id=output_id, limit=limit)]


@app.get("/monitoring/status", response_model=MonitoringStatus)
def monitoring_status() -> MonitoringStatus:
    return MonitoringStatus(
        running=state.running,
        calibrated=state.calibrated,
        simulated_now=state.now,
        server_time=datetime.now(timezone.utc),
        interval_seconds=state.source.get_interval().total_seconds(),
        seconds_per_tick=state.seconds_per_tick,
        outputs=state.source.list_outputs(),
    )


@app.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request) -> LoginResponse:
    """Échange un identifiant et un mot de passe contre un jeton de session à durée limitée."""
    settings = auth.load_settings()
    client = _client(request)
    if _throttle.blocked(body.username, client):
        raise HTTPException(
            status_code=429, detail="Trop d'essais : réessayer dans quelques minutes", headers={"Retry-After": "300"}
        )
    if not auth.authenticate(settings, body.username, body.password.get_secret_value()):
        _throttle.record_failure(body.username, client)
        logger.warning("Échec de connexion pour %r depuis %s", body.username, client)
        raise HTTPException(status_code=401, detail="Identifiant ou mot de passe incorrect")
    _throttle.reset(body.username, client)
    logger.info("Connexion de %s", body.username)
    return LoginResponse(
        access_token=auth.issue_token(body.username, settings.secret, settings.ttl),
        expires_in=int(settings.ttl.total_seconds()),
        username=body.username,
    )


@app.post("/monitoring/start", response_model=MonitoringStatus)
def start_monitoring(subject: Annotated[str, Depends(require_token)]) -> MonitoringStatus:
    logger.info("Monitoring démarré par %s", subject)
    state.start()
    return monitoring_status()


@app.post("/monitoring/stop", response_model=MonitoringStatus)
def stop_monitoring(subject: Annotated[str, Depends(require_token)]) -> MonitoringStatus:
    logger.info("Monitoring arrêté par %s", subject)
    state.stop()
    return monitoring_status()


@app.get("/config", response_model=AppConfig)
def get_config() -> AppConfig:
    return state.config


@app.put("/config", response_model=AppConfig)
def update_config(update: ConfigUpdate, subject: Annotated[str, Depends(require_token)]) -> AppConfig:
    """
    Met à jour à chaud le sous-ensemble modifiable de la configuration
    (§35). Reconstruit un AppConfig complet et le valide avant toute prise
    d'effet (jamais d'affectation directe d'attribut, qui contournerait les
    validateurs, ex. l'ordre close < warning < open).
    """
    candidate = state.config.model_dump()
    incident_data = candidate["incident"]
    if update.open_score is not None:
        incident_data["open_score"] = update.open_score
    if update.close_score is not None:
        incident_data["close_score"] = update.close_score
    if update.warning_score is not None:
        incident_data["warning_score"] = update.warning_score
    if update.minimum_duration_seconds is not None:
        incident_data["minimum_duration"] = timedelta(seconds=update.minimum_duration_seconds)
    if update.recovery_duration_seconds is not None:
        incident_data["recovery_duration"] = timedelta(seconds=update.recovery_duration_seconds)
    if update.critical_score is not None:
        candidate["alerts"]["critical_score"] = update.critical_score

    try:
        validated = AppConfig.model_validate(candidate)
    except Exception as exc:  # ValidationError Pydantic
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Mutation en place des sous-modèles (pas remplacement de `state.config`
    # lui-même) : `state.loop._config` et `state.config` restent le même
    # objet, comme avant ce correctif — seule la validation est désormais
    # complète (AppConfig entier, pas juste IncidentConfig).
    logger.info("Configuration modifiée par %s : %s", subject, update.model_dump(exclude_none=True))
    state.config.incident = validated.incident
    state.config.alerts = validated.alerts
    state.loop.incident_manager.update_config(validated.incident, critical_score=validated.alerts.critical_score)

    return state.config
