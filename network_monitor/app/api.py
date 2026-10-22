"""
API FastAPI : expose le système de monitoring en HTTP.

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

import hmac
import logging
import os
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, ValidationError

from network_monitor.analysis.availability import build_report
from network_monitor.app import auth
from network_monitor.app.config import AppConfig, HistoryConfig
from network_monitor.app.http_cache import ETagMiddleware
from network_monitor.app.lock import SingleInstanceLock
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.metrics import Family, render
from network_monitor.app.models import (
    AnomalyResult,
    ForecastResult,
    Incident,
    IncidentStatus,
    IncidentVerdict,
    MaintenanceWindow,
    Severity,
    TrafficSample,
)
from network_monitor.app.notify import Heartbeat, build_dispatcher
from network_monitor.data.sources import build_source
from network_monitor.incidents.manager import Alert
from network_monitor.storage.archive import ParquetArchive
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
# sur la Seasonal Baseline, nettement plus rapide. Un déploiement réel
# peut toujours passer AppConfig.defaults() explicitement à MonitoringState.
#
# Ne jamais muter cet objet en place : chaque MonitoringState en prend une
# copie (voir __init__) pour qu'un PUT /config sur une instance ne modifie
# jamais ce gabarit partagé par toutes les instances du process (tests inclus).
DEFAULT_API_CONFIG = AppConfig(history=HistoryConfig(minimum=timedelta(days=30), preferred=timedelta(days=40)))

_MAX_WINDOW_SPAN = timedelta(days=31)
"""Plage maximale pour un `since`/`until` explicite : évite qu'une
requête mal bornée ne tente de sérialiser des mois de mesures en une fois."""


def require_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    """
    Dépendance FastAPI protégeant les endpoints de contrôle. Renvoie l'identité
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


def require_role(minimum: auth.Role) -> Callable[..., str]:
    """Dépendance : une identité authentifiée **et** au moins le rôle demandé (403 sinon). Le rôle est relu dans `NM_USERS` à chaque
    requête : rétrograder un compte s'applique dès le redémarrage, sans attendre l'expiration de ses sessions, et la session d'un compte
    retiré est refusée (401)."""

    def dependency(subject: Annotated[str, Depends(require_token)]) -> str:
        role = auth.role_of(auth.load_settings(), subject)
        if role is None:
            raise HTTPException(status_code=401, detail="Ce compte n'existe plus : se reconnecter")
        if role < minimum:
            logger.warning("Droits insuffisants pour %s (rôle %s, rôle requis %s)", subject, role.name.lower(), minimum.name.lower())
            raise HTTPException(
                status_code=403,
                detail=f"Droits insuffisants : le rôle « {minimum.name.lower()} » est requis (le vôtre : « {role.name.lower()} »)",
            )
        return subject

    return dependency


ViewerSubject = Annotated[str, Depends(require_role(auth.Role.VIEWER))]
OperatorSubject = Annotated[str, Depends(require_role(auth.Role.OPERATOR))]
AdminSubject = Annotated[str, Depends(require_role(auth.Role.ADMIN))]


def _client(request: Request) -> str:
    # Derrière un proxy, c'est l'adresse du proxy : le limiteur de connexion est alors global.
    return request.client.host if request.client else "inconnu"


# --- État du service : configuration, boucle, thread d'exécution ---


@dataclass
class Telemetry:
    """Ce que la boucle sait d'elle-même, pour `/metrics` et `/health/ready`."""

    ticks: int = 0
    errors: int = 0
    last_tick_at: float | None = None
    """Heure réelle (epoch) de la fin du dernier pas, réussi ou non."""
    last_tick_seconds: float | None = None


_TICK_MARGIN_SECONDS = 10.0
"""Marge ajoutée à 3 périodes avant de dire qu'un pas manque : un pas lent n'est pas une panne."""


class MonitoringState:
    """
    Regroupe tout ce que l'API doit garder en mémoire entre les requêtes.
    La boucle de monitoring tourne dans un thread dédié : `run_forever`
    (app/main.py) est bloquant et n'a pas de notion de rythme réel, donc
    l'API pilote elle-même l'avancée du temps simulé, à un rythme accéléré
    (`seconds_per_tick`) adapté à une démonstration plutôt qu'aux 5 minutes
    réelles d'un déploiement.
    """

    def __init__(self, config: AppConfig | None = None, db_url: str | None = None) -> None:
        # Copie profonde : cette instance ne doit jamais pouvoir muter
        # DEFAULT_API_CONFIG (ni la config passée par l'appelant), qu'elle
        # vienne d'ici ou d'un futur PUT /config.
        self.config = (config or DEFAULT_API_CONFIG).model_copy(deep=True)
        archive = ParquetArchive(self.config.storage.archive_dir) if self.config.storage.archive_dir else None
        db_url = db_url or self.config.storage.database_url
        self._lock_path = self.config.storage.lock_file or (
            db_url.removeprefix("sqlite:///") + ".lock" if db_url.startswith("sqlite:///") else "network_monitor.lock"
        )
        self.lock = SingleInstanceLock(self._lock_path)
        self.storage: Repository = SqlAlchemyRepository(db_url, archive)
        self.source = build_source(self.config)
        self.live = bool(getattr(self.source, "live", False))
        # Un secret manquant lève ici : refuser de démarrer vaut mieux que découvrir pendant
        # un incident qu'aucune alerte ne part.
        self.notifier = build_dispatcher(self.config.notifications, self.storage)
        self.heartbeat = Heartbeat(self.config.heartbeat, self.readiness) if self.config.heartbeat.enabled else None
        self.loop = MonitoringLoop(self.source, self.storage, self.config, self.notifier)
        interval = self.config.monitoring.interval
        # Source « live » (SQL) : horloge réelle, un tick par intervalle. Rejeu CSV :
        # horloge simulée qui démarre à la bascule historique → rejeu du fichier.
        # Mock : horloge simulée accélérée, calée sur l'heure actuelle.
        if hasattr(self.source, "default_start"):  # rejeu d'un fichier (CSV, Parquet)
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
        self.telemetry = Telemetry()
        self._started_at = time.time()

    def readiness(self) -> list[str]:
        """Raisons pour lesquelles le service ne fait pas son travail (liste vide = sain). Distincte de `/health`,
        qui dit seulement que le processus répond : une boucle arrêtée ou des données périmées ne se voient pas là."""
        reasons: list[str] = []
        if not self.storage.ping():
            reasons.append("base de données inaccessible")
        if not self.running:
            return [*reasons, "boucle de surveillance arrêtée"]
        limit = 3 * self.seconds_per_tick + _TICK_MARGIN_SECONDS
        age = time.time() - (self.telemetry.last_tick_at or self._started_at)
        if age > limit:
            reasons.append(f"aucun pas de surveillance depuis {age:.0f} s (limite {limit:.0f} s)")
        if self.telemetry.ticks:  # avant le premier pas, aucune mesure n'est encore attendue
            stale_after = 3 * self.source.get_interval()
            for output_id in self.source.list_outputs():
                last = self.loop.last_sample_at.get(output_id)
                if last is None:
                    reasons.append(f"aucune mesure reçue pour {output_id}")
                elif self.now - last > stale_after:
                    reasons.append(f"données périmées pour {output_id} (dernière mesure : {last:%Y-%m-%d %H:%M} UTC)")
        return reasons

    def calibrate(self) -> None:
        self.loop.calibrate(self.now)
        self.calibrated = True

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._started_at = time.time()
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
            began = time.monotonic()
            try:
                self.loop.run_tick(now)
            except Exception:  # ne jamais laisser une erreur ponctuelle tuer la boucle
                self.telemetry.errors += 1
                logger.exception("Erreur pendant run_tick à %s", now)
            self.telemetry.ticks += 1
            self.telemetry.last_tick_seconds = time.monotonic() - began
            self.telemetry.last_tick_at = time.time()


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
    settings = auth.load_settings()  # une configuration d'authentification mal formée empêche de démarrer (AuthConfigError), au lieu de se découvrir à la première requête
    if settings.legacy_users:
        logger.warning(
            "NM_USERS : %s déclaré(s) sans rôle (ancien format) : droits d'administrateur. Déclarer un rôle : "
            '{"nom": {"hash": "...", "role": "operator"}}.', ", ".join(settings.legacy_users),
        )
    state.lock.acquire()  # refuse de démarrer si une autre instance tient déjà la base
    if not _is_development and state.config.source.type == "mock":
        logger.warning(
            "NM_ENV=%s mais la source de données est « mock » : le dashboard affichera des données SIMULÉES. "
            "Fournir une vraie configuration (NM_CONFIG, source.type).", os.environ.get("NM_ENV")
        )
    state.calibrate()
    state.start()
    if state.notifier:
        state.notifier.start()
    if state.heartbeat:
        state.heartbeat.start()
    yield
    if state.heartbeat:  # d'abord : un arrêt voulu ne doit pas se signaler comme une panne
        state.heartbeat.stop()
    state.stop()
    if state.notifier:
        state.notifier.stop()
    state.lock.release()


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

# Ordre : le dernier ajouté est le plus extérieur. ETag (corps non compressé) → gzip → CORS.
app.add_middleware(ETagMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Authorization", "Content-Type"],
)


# --- Schémas de réponse spécifiques à l'API (au-delà des modèles du cœur métier) ---


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
    horloge locale, qui peut être en avance ou en retard."""
    interval_seconds: float
    seconds_per_tick: float
    outputs: list[str]


class ConfigUpdate(BaseModel):
    """Sous-ensemble modifiable à chaud de la configuration."""

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
    role: str
    """`viewer`, `operator` ou `admin` : sert à l'interface à n'afficher que ce que le compte peut faire (le serveur contrôle de toute façon)."""


class AlertOut(BaseModel):
    """Représentation API d'une alerte."""

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
    suppressed: str | None = None
    root_cause_incident_id: str | None = None
    impacted_outputs: list[str] = Field(default_factory=list)

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
            suppressed=alert.suppressed,
            root_cause_incident_id=alert.root_cause_incident_id,
            impacted_outputs=alert.impacted_outputs,
        )


# --- Endpoints ---


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    """200 si le service fait son travail, 503 avec les raisons sinon (boucle arrêtée ou bloquée, données
    périmées, base inaccessible). `/health` reste la simple vivacité : c'est lui que sonde le HEALTHCHECK Docker,
    car un arrêt volontaire de la surveillance ne doit pas faire redémarrer le conteneur."""
    reasons = state.readiness()
    return JSONResponse(status_code=503 if reasons else 200, content={"status": "unavailable" if reasons else "ok", "reasons": reasons})


def _gauge(name: str, help_text: str, value: float) -> Family:
    return (name, help_text, "gauge", [({}, value)])


_ACTIVE_STATUSES = ("suspect", "open", "stale")
_NOTIFICATION_STATUSES = ("pending", "sent", "failed")


@app.get("/metrics")
def metrics(authorization: Annotated[str | None, Header()] = None) -> Response:
    """Métriques au format texte Prometheus. Ouvert comme les autres lectures, sauf si `NM_METRICS_TOKEN` est défini
    (jeton porteur)."""
    token = os.environ.get("NM_METRICS_TOKEN")
    if token and not (authorization and hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())):
        raise HTTPException(status_code=401, detail="Jeton de métriques invalide ou absent", headers={"WWW-Authenticate": "Bearer"})
    t, outputs = state.telemetry, state.source.list_outputs()
    db_ok = state.storage.ping()
    families: list[Family] = [
        _gauge("nm_up", "Le processus répond.", 1),
        ("nm_build_info", "Version du logiciel.", "gauge", [({"version": app.version}, 1)]),
        _gauge("nm_monitoring_running", "1 si la boucle de surveillance tourne.", state.running),
        _gauge("nm_interval_seconds", "Intervalle entre deux mesures.", state.source.get_interval().total_seconds()),
        _gauge("nm_simulated_clock_timestamp_seconds", "Horloge du serveur (simulée avec le Mock), en secondes epoch.", state.now.timestamp()),
        ("nm_ticks_total", "Pas de surveillance exécutés.", "counter", [({}, t.ticks)]),
        ("nm_tick_errors_total", "Pas de surveillance terminés par une erreur.", "counter", [({}, t.errors)]),
        ("nm_output_calibrated", "1 si la sortie a un modèle calibré.", "gauge", [({"output": o}, state.loop.is_calibrated(o)) for o in outputs]),
        ("nm_output_last_sample_timestamp_seconds", "Horodatage de la dernière mesure reçue, par sortie.", "gauge",
         [({"output": o}, state.loop.last_sample_at[o].timestamp()) for o in outputs if o in state.loop.last_sample_at]),
        _gauge("nm_database_ok", "1 si la base répond.", db_ok),
    ]
    if t.last_tick_at is not None and t.last_tick_seconds is not None:
        families += [
            _gauge("nm_last_tick_timestamp_seconds", "Heure réelle de la fin du dernier pas, en secondes epoch.", t.last_tick_at),
            _gauge("nm_last_tick_duration_seconds", "Durée du dernier pas.", t.last_tick_seconds),
        ]
    if db_ok:
        active = Counter((i.output_id, i.status.value) for i in state.storage.list_active_incidents())
        families.append(("nm_active_incidents", "Incidents en cours, par sortie et statut.", "gauge",
                         [({"output": o, "status": s}, active[(o, s)]) for o in outputs for s in _ACTIVE_STATUSES]))
        sent = state.storage.count_notifications()
        channels = [c.name for c in state.config.notifications.channels]
        families.append(("nm_notifications", "Notifications par canal et statut (comptes en base).", "gauge",
                         [({"channel": c, "status": s}, sent.get((c, s), 0)) for c in channels for s in _NOTIFICATION_STATUSES]))
    return Response(render(families), media_type="text/plain; version=0.0.4; charset=utf-8")


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


def _require_aware(*values: datetime | None) -> None:
    for value in values:
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail="les dates doivent inclure un fuseau horaire (suffixe Z ou +00:00)")


def _parse_window(
    since: datetime | None, until: datetime | None, default_span: timedelta
) -> tuple[datetime, datetime]:
    for label, value in (("since", since), ("until", until)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(
                status_code=422,
                detail=f"`{label}` doit inclure un fuseau horaire (ex. suffixe Z ou +00:00)",
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


_NEAREST_BEFORE, _NEAREST_AFTER = timedelta(minutes=35), timedelta(minutes=10)
"""Tolérance de recherche autour d'un instant : au-delà, on dit « pas de mesure » plutôt que d'en inventer une."""
_MAX_NEAREST_INSTANTS = 16


@app.get("/outputs/nearest", response_model=dict[str, dict[str, TrafficSample | None]])
def outputs_nearest(
    ago_minutes: Annotated[
        list[Annotated[int, Field(ge=0, le=_MAX_WINDOW_SPAN.days * 24 * 60)]], Query(min_length=1, max_length=_MAX_NEAREST_INSTANTS)
    ],
) -> dict[str, dict[str, TrafficSample | None]]:
    """Pour chaque sortie et chaque `ago_minutes` (relatif à l'heure *serveur*), la mesure la plus proche de
    `state.now - ago` dans [-35 min, +10 min], ou `null`. Remplace, pour le tableau comparatif, une requête
    `history` par sortie et par instant (24 requêtes par rafraîchissement) par une seule."""
    instants = sorted(set(ago_minutes))
    result: dict[str, dict[str, TrafficSample | None]] = {}
    for output_id in state.source.list_outputs():
        found: dict[str, TrafficSample | None] = {}
        for ago in instants:
            target = state.now - timedelta(minutes=ago)
            samples = state.storage.get_history(output_id, target - _NEAREST_BEFORE, target + _NEAREST_AFTER)
            found[str(ago)] = min(samples, key=lambda x: abs(x.timestamp - target), default=None)
        result[output_id] = found
    return result


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
    (l'horloge simulée avance à un rythme différent de l'horloge réelle)."""
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
    Prévision à la demande au-delà de l'instant simulé courant —
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


class FeedbackIn(BaseModel):
    verdict: IncidentVerdict | None
    """`null` retire le jugement."""
    note: str | None = Field(default=None, max_length=500)


@app.put("/incidents/{incident_id}/feedback", response_model=Incident)
def set_incident_feedback(
    incident_id: str, body: FeedbackIn, subject: OperatorSubject
) -> Incident:
    """Un opérateur juge un incident confirmé : vrai ou faux positif. C'est la seule vérité terrain disponible
    en exploitation : elle alimente la précision mesurée (`/reports/precision`), exclut les faux positifs du
    rapport de disponibilité et sert à re-régler les seuils (`analysis.evaluation --from-db`)."""
    incident = state.storage.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"Incident inconnu : {incident_id}")
    if incident.opened_at is None:
        raise HTTPException(status_code=409, detail="Seul un incident confirmé peut être jugé")
    state.storage.set_feedback(incident_id, body.verdict, subject, state.now, body.note)
    logger.info("Incident %s jugé %s par %s", incident_id, body.verdict, subject)
    return state.storage.get_incident(incident_id) or incident


def _active_confirmed_incident(incident_id: str) -> Incident:
    incident = state.storage.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"Incident inconnu : {incident_id}")
    if incident.opened_at is None or incident.status not in (IncidentStatus.OPEN, IncidentStatus.STALE):
        raise HTTPException(status_code=409, detail="Seul un incident confirmé et en cours peut être pris en charge")
    return incident


@app.put("/incidents/{incident_id}/ack", response_model=Incident)
def acknowledge_incident(incident_id: str, subject: OperatorSubject) -> Incident:
    """« Je m'en occupe » : l'incident n'est plus escaladé (`notifications.escalate_after`). Une prise en charge déjà
    enregistrée est remplacée (la dernière personne qui dit s'en occuper l'emporte)."""
    incident = _active_confirmed_incident(incident_id)
    state.storage.set_ack(incident_id, subject, state.now)
    logger.info("Incident %s pris en charge par %s", incident_id, subject)
    return state.storage.get_incident(incident_id) or incident


@app.delete("/incidents/{incident_id}/ack", response_model=Incident)
def release_incident(incident_id: str, subject: OperatorSubject) -> Incident:
    """Retire la prise en charge : le délai d'escalade repart de l'ouverture de l'incident, pas de ce retrait."""
    incident = state.storage.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"Incident inconnu : {incident_id}")
    state.storage.set_ack(incident_id, None, state.now)
    logger.info("Prise en charge de l'incident %s retirée par %s", incident_id, subject)
    return state.storage.get_incident(incident_id) or incident


@app.get("/reports/precision")
def precision_report(
    days: Annotated[float, Query(gt=0, le=366)] = 30,
    output_id: Annotated[str | None, Query()] = None,
) -> dict:
    """Précision **mesurée** : parmi les incidents confirmés des N derniers jours (horloge du serveur) que des
    opérateurs ont jugés, quelle part était de vrais incidents. Les incidents sans verdict ne comptent pas :
    `judged_share` dit quelle part a été jugée, donc à quel point la précision est représentative."""
    if output_id is not None:
        _ensure_known_output(output_id)
    stop = state.now
    incidents = [
        i for i in state.storage.list_incidents_between(stop - timedelta(days=days), stop)
        if output_id is None or i.output_id == output_id
    ]

    def summary(items: list[Incident]) -> dict:
        tp = sum(i.verdict == IncidentVerdict.TRUE_POSITIVE for i in items)
        fp = sum(i.verdict == IncidentVerdict.FALSE_POSITIVE for i in items)
        return {
            "confirmed": len(items), "true_positives": tp, "false_positives": fp, "unjudged": len(items) - tp - fp,
            "precision_percent": round(100 * tp / (tp + fp), 1) if tp + fp else None,
            "judged_share_percent": round(100 * (tp + fp) / len(items), 1) if items else None,
        }

    groups: dict[str, dict[str, list[Incident]]] = {"by_output": {}, "by_type": {}}
    for i in incidents:
        groups["by_output"].setdefault(i.output_id, []).append(i)
        groups["by_type"].setdefault(i.incident_type.value, []).append(i)
    return {
        "days": days, **summary(incidents),
        "by_output": {k: summary(v) for k, v in sorted(groups["by_output"].items())},
        "by_type": {k: summary(v) for k, v in sorted(groups["by_type"].items())},
    }


class MaintenanceOut(BaseModel):
    id: str
    output_id: str | None
    start: datetime
    end: datetime
    reason: str
    created_by: str
    cancelled_at: datetime | None
    state: Literal["upcoming", "active", "past", "cancelled"]


class MaintenanceIn(BaseModel):
    """Soit `start` et `end` (avec fuseau), soit `starts_in_minutes` et `duration_minutes` (relatifs à
    l'horloge du **serveur**, pour un client dont l'horloge n'est pas la sienne)."""

    output_id: str | None = None
    """None = toutes les sorties."""
    reason: str = Field(min_length=1, max_length=200)
    start: datetime | None = None
    end: datetime | None = None
    starts_in_minutes: float | None = Field(default=None, ge=0, le=60 * 24 * 30)
    duration_minutes: float | None = Field(default=None, gt=0, le=60 * 24 * 14)


def _maintenance_out(w: MaintenanceWindow, now: datetime) -> MaintenanceOut:
    status: Literal["upcoming", "active", "past", "cancelled"] = (
        "cancelled" if w.cancelled_at else "past" if w.end <= now else "upcoming" if w.start > now else "active"
    )
    return MaintenanceOut(
        id=w.id, output_id=w.output_id, start=w.start, end=w.end, reason=w.reason,
        created_by=w.created_by, cancelled_at=w.cancelled_at, state=status,
    )


@app.get("/maintenance", response_model=list[MaintenanceOut])
def list_maintenance(days: Annotated[float, Query(gt=0, le=366)] = 7) -> list[MaintenanceOut]:
    """Fenêtres en cours, à venir, et terminées ou annulées depuis `days` jours (horloge du serveur)."""
    now = state.now
    windows = state.storage.list_maintenance(now - timedelta(days=days), now + timedelta(days=366), include_cancelled=True)
    return [_maintenance_out(w, now) for w in windows]


@app.post("/maintenance", response_model=MaintenanceOut, status_code=201)
def create_maintenance(body: MaintenanceIn, subject: OperatorSubject) -> MaintenanceOut:
    """Planifie une maintenance : pendant la fenêtre, les incidents de la sortie sont enregistrés mais ne
    notifient personne ; son temps est exclu de la disponibilité ; la recalibration l'ignore."""
    _require_aware(body.start, body.end)
    absolute = body.start is not None or body.end is not None
    relative = body.starts_in_minutes is not None or body.duration_minutes is not None
    if absolute == relative:
        raise HTTPException(status_code=422, detail="Donner soit `start` et `end`, soit `starts_in_minutes` et `duration_minutes`")
    now = state.now
    if absolute:
        if body.start is None or body.end is None:
            raise HTTPException(status_code=422, detail="`start` et `end` vont ensemble")
        start, end = body.start, body.end
    else:
        if body.duration_minutes is None:
            raise HTTPException(status_code=422, detail="`duration_minutes` est requis")
        start = now + timedelta(minutes=body.starts_in_minutes or 0)
        end = start + timedelta(minutes=body.duration_minutes)
    if end <= start:
        raise HTTPException(status_code=422, detail="la fin doit suivre le début")
    if end - start > timedelta(days=14):
        raise HTTPException(status_code=422, detail="une fenêtre ne dépasse pas 14 jours (en planifier plusieurs)")
    if end <= now:
        raise HTTPException(status_code=422, detail="cette fenêtre est déjà terminée")
    if body.output_id is not None:
        _ensure_known_output(body.output_id)
    window = MaintenanceWindow(
        id=str(uuid.uuid4()), output_id=body.output_id, start=start, end=end, reason=body.reason,
        created_by=subject, created_at=now,
    )
    state.storage.add_maintenance(window)
    logger.info("Maintenance %s sur %s de %s à %s créée par %s : %s", window.id, body.output_id or "toutes les sorties", start, end, subject, body.reason)
    return _maintenance_out(window, now)


@app.delete("/maintenance/{window_id}", response_model=MaintenanceOut)
def cancel_maintenance(window_id: str, subject: OperatorSubject) -> MaintenanceOut:
    """Annule une fenêtre (conservée pour l'audit). Un incident en cours qui était retenu par elle
    est annoncé au tick suivant."""
    now = state.now
    if not state.storage.cancel_maintenance(window_id, now):
        raise HTTPException(status_code=404, detail=f"Fenêtre inconnue ou déjà annulée : {window_id}")
    logger.info("Maintenance %s annulée par %s", window_id, subject)
    window = next((w for w in state.storage.list_maintenance(now - timedelta(days=366), now + timedelta(days=366), include_cancelled=True) if w.id == window_id), None)
    assert window is not None
    return _maintenance_out(window, now)


@app.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    output_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    all: Annotated[bool, Query(description="inclut les signaux faibles (suspect, retour à la normale, non confirmés)")] = False,
) -> list[AlertOut]:
    """Historique des alertes émises, les plus récentes d'abord. Par défaut, seulement les
    alertes qui ont compté : un signal faible résorbé sans jamais avoir été confirmé n'en est pas une
    (`all=true` pour tout voir : audit, mise au point des seuils)."""
    if output_id is not None:
        _ensure_known_output(output_id)
    return [AlertOut.from_alert(a) for a in state.storage.list_alerts(output_id=output_id, limit=limit, notable_only=not all)]


_MAX_REPORT_SPAN = timedelta(days=366)


@app.get("/reports/availability")
def availability_report(
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    output_id: Annotated[str | None, Query()] = None,
    downtime_severity: Annotated[Severity, Query()] = Severity.CRITICAL,
    include_false_positives: Annotated[bool, Query()] = False,
    format: Annotated[Literal["json", "csv"], Query()] = "json",
    days: Annotated[float | None, Query(gt=0, le=366)] = None,
):
    """
    Rapport de disponibilité par sortie sur une période (30 jours se terminant maintenant par
    défaut ; `days` donne les N derniers jours selon l'horloge du **serveur**, ce que doit utiliser
    un client dont l'horloge n'est pas celle du serveur) : indisponibilité, dégradation, couverture des mesures, MTTD et MTTR, calculés sur les
    dates **rétro-datées** des incidents (début réel, fin réelle). `format=csv` pour un tableur.
    """
    _require_aware(start, end)
    if days is not None and (start is not None or end is not None):
        raise HTTPException(status_code=422, detail="`days` est exclusif avec `start` et `end`")
    horizon = state.now
    stop = end or horizon
    begin = start or (stop - timedelta(days=days or 30))
    if begin >= stop:
        raise HTTPException(status_code=400, detail="`start` doit être antérieur à `end`")
    if stop - begin > _MAX_REPORT_SPAN:
        raise HTTPException(status_code=400, detail="période limitée à 366 jours")
    if output_id is not None:
        _ensure_known_output(output_id)
    outputs = [output_id] if output_id else state.source.list_outputs()
    # Les mesures brutes sont purgées au-delà de `history.preferred` : la période observée ne remonte
    # pas plus loin que la première mesure conservée (voir analysis/availability.py).
    stats = state.storage.sample_stats(begin, min(stop, horizon + state.source.get_interval()))
    report = build_report(
        state.storage.list_incidents_between(begin, stop), outputs, begin, stop, horizon,
        state.source.get_interval(), stats, downtime_severity,
        maintenance=state.storage.list_maintenance(begin, stop), include_false_positives=include_false_positives,
    )
    if format == "csv":
        stamp = f"{begin:%Y%m%d}-{stop:%Y%m%d}"
        return Response(
            report.to_csv(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="disponibilite-{stamp}.csv"'},
        )
    return asdict(report)


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


class NotificationOut(BaseModel):
    id: str
    channel: str
    status: str
    attempts: int
    created_at: datetime
    sent_at: datetime | None
    last_error: str | None
    summary: str


@app.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    _subject: ViewerSubject,
    status: Annotated[Literal["pending", "sent", "failed"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[NotificationOut]:
    """État des envois (réservé aux utilisateurs connectés : les erreurs peuvent citer le service distant)."""
    return [
        NotificationOut(
            id=n.id, channel=n.channel, status=n.status, attempts=n.attempts, created_at=n.created_at,
            sent_at=n.sent_at, last_error=n.last_error,
            summary=f"{n.alert['output_id']} {n.alert['event']} ({n.alert['incident_type']})",
        )
        for n in state.storage.list_notifications(status, limit)
    ]


@app.post("/notifications/test/{channel}")
def test_notification(channel: str, subject: AdminSubject) -> dict:
    """Envoie un message d'essai immédiat sur un canal (vérifier un groupe Telegram, un numéro, un webhook)."""
    if state.notifier is None or channel not in state.notifier.channels:
        raise HTTPException(status_code=404, detail=f"Canal inconnu ou notifications désactivées : {channel}")
    logger.info("Message d'essai sur %s demandé par %s", channel, subject)
    results = state.notifier.send_test(channel)
    return {"channel": channel, "ok": all(v is None for v in results.values()), "results": results}


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
        role=settings.roles[body.username].name.lower(),
    )


@app.post("/monitoring/start", response_model=MonitoringStatus)
def start_monitoring(subject: AdminSubject) -> MonitoringStatus:
    logger.info("Monitoring démarré par %s", subject)
    state.start()
    return monitoring_status()


@app.post("/monitoring/stop", response_model=MonitoringStatus)
def stop_monitoring(subject: AdminSubject) -> MonitoringStatus:
    logger.info("Monitoring arrêté par %s", subject)
    state.stop()
    return monitoring_status()


@app.get("/config", response_model=AppConfig)
def get_config() -> AppConfig:
    return state.config


@app.put("/config", response_model=AppConfig)
def update_config(update: ConfigUpdate, subject: AdminSubject) -> AppConfig:
    """
    Met à jour à chaud le sous-ensemble modifiable de la configuration. Reconstruit un AppConfig complet et le valide avant toute prise
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
    except ValidationError as exc:
        # Messages de validation structurés, sans la valeur saisie ni l'URL de documentation Pydantic.
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_input=False, include_context=False)
        ) from exc

    # Mutation en place des sous-modèles (pas remplacement de `state.config`
    # lui-même) : `state.loop._config` et `state.config` restent le même
    # objet, comme avant ce correctif — seule la validation est désormais
    # complète (AppConfig entier, pas juste IncidentConfig).
    logger.info("Configuration modifiée par %s : %s", subject, update.model_dump(exclude_none=True))
    state.config.incident = validated.incident
    state.config.alerts = validated.alerts
    state.loop.incident_manager.update_config(validated.incident, critical_score=validated.alerts.critical_score)

    return state.config


# Interface web servie par l'API elle-même (image Docker : une seule origine, donc pas de CORS à
# régler, et un seul processus à déployer). Doit rester **après** toutes les routes : ce montage
# répond à tout ce qu'aucune route n'a reconnu.
_static_dir = os.environ.get("NM_STATIC_DIR")
if _static_dir and Path(_static_dir).is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="frontend")
