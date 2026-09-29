"""
API FastAPI (§37) : expose le système de monitoring en HTTP.

Couche fine au-dessus de MonitoringLoop / Repository / IncidentManager —
aucune logique métier propre ici, uniquement sérialisation et orchestration
du cycle de vie (démarrage/arrêt de la boucle en tâche de fond).

Sécurité : CORS restreint par défaut aux origines de développement locales
du frontend Vite ; à étendre via NETWORK_MONITOR_CORS_ORIGINS (liste séparée
par des virgules) pour un autre hôte. Cette API ne comporte AUCUNE
authentification : ne pas l'exposer au-delà de localhost sans ajouter une
couche d'authentification devant (REVUE.md, S-1/S-2).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from network_monitor.app.config import AppConfig, IncidentConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import Alert, AnomalyResult, ForecastResult, Incident, TrafficSample
from network_monitor.data.mock import MockDataSource
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
DEFAULT_API_CONFIG = AppConfig(history={"minimum": "30d", "preferred": "40d"})


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
        self.config = config or DEFAULT_API_CONFIG
        self.storage: Repository = SqlAlchemyRepository(db_url)
        self.source = MockDataSource(interval=self.config.monitoring.interval)
        self.loop = MonitoringLoop(self.source, self.storage, self.config)
        self.now = datetime.now(UTC)
        self.seconds_per_tick = 2.0
        self.calibrated = False
        self.running = False
        self._clock_started = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def calibrate(self) -> None:
        self.loop.calibrate(self.now)
        self.calibrated = True

    def start(self) -> None:
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
        self._stop.set()
        thread = self._thread
        # join() : sans lui, un start() immédiat après stop() pouvait lancer un
        # second thread pendant que le premier finissait encore son tick
        # (deux threads sur le même MockDataSource, REVUE.md Conc-2). Le
        # timeout évite de bloquer indéfiniment si un tick est très long.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10.0)
        self._thread = None
        self.running = False

    def _run(self) -> None:
        interval = self.source.get_interval()
        while not self._stop.is_set():
            self.now = self.now + interval
            try:
                self.loop.run_tick(self.now)
            except Exception:  # ne jamais laisser une erreur ponctuelle tuer la boucle
                logger.exception("Erreur pendant run_tick à %s", self.now)
            # wait() (et non sleep) : réveille immédiatement le thread dès que stop() est appelé.
            self._stop.wait(self.seconds_per_tick)


state = MonitoringState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.calibrate()
    state.start()
    yield
    state.stop()
    state.storage.dispose()


app = FastAPI(
    title="Network Monitor API",
    description="Détection d'anomalies réseau non supervisée — voir le cahier des charges du projet.",
    version="1.0.0",
    lifespan=lifespan,
)
_DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
_cors_origins = [
    o.strip()
    for o in os.environ.get("NETWORK_MONITOR_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
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


# Borne maximale d'une fenêtre demandée : sans elle, une requête sur plusieurs
# années chargerait l'intégralité de la table en mémoire (REVUE.md, S-5).
MAX_WINDOW_SPAN = timedelta(days=31)


def _as_aware(dt: datetime) -> datetime:
    """Un datetime naïf est interprété comme UTC (au lieu d'une comparaison qui lèverait un TypeError → 500)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_window(since: datetime | None, until: datetime | None, default_span: timedelta) -> tuple[datetime, datetime]:
    end = _as_aware(until) if until else state.now
    start = _as_aware(since) if since else (end - default_span)
    if start >= end:
        raise HTTPException(status_code=400, detail="`since` doit être antérieur à `until`")
    if end - start > MAX_WINDOW_SPAN:
        raise HTTPException(
            status_code=400,
            detail=f"Fenêtre trop large (maximum {MAX_WINDOW_SPAN.days} jours)",
        )
    return start, end


def _ensure_known_output(output_id: str) -> None:
    if output_id not in state.source.list_outputs():
        raise HTTPException(status_code=404, detail=f"Sortie inconnue : {output_id}")


@app.get("/outputs/{output_id}/history", response_model=list[TrafficSample])
def output_history(
    output_id: str,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
) -> list[TrafficSample]:
    _ensure_known_output(output_id)
    start, end = _parse_window(since, until, timedelta(hours=12))
    return state.storage.get_history(output_id, start, end)


@app.get("/outputs/{output_id}/anomalies", response_model=list[AnomalyResult])
def output_anomalies(
    output_id: str,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
) -> list[AnomalyResult]:
    _ensure_known_output(output_id)
    start, end = _parse_window(since, until, timedelta(hours=12))
    return state.storage.get_anomalies(output_id, start, end)


@app.get("/outputs/{output_id}/forecast", response_model=list[ForecastResult])
def output_forecast(
    output_id: str,
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
) -> list[ForecastResult]:
    _ensure_known_output(output_id)
    start, end = _parse_window(since, until, timedelta(hours=12))
    return state.storage.get_forecasts(output_id, start, end)


@app.get("/outputs/{output_id}/forecast/ahead", response_model=list[ForecastResult])
def output_forecast_ahead(
    output_id: str,
    horizon_minutes: int | None = Query(default=None, ge=5, le=24 * 60),
) -> list[ForecastResult]:
    """
    Prévision à la demande au-delà de l'instant simulé courant (§13) —
    alimente la courbe "Prédiction" du dashboard, calculée à la volée à
    partir du modèle déjà calibré, sans rien persister.
    """
    _ensure_known_output(output_id)
    try:
        return state.loop.forecast_ahead(output_id, state.now, (timedelta(minutes=horizon_minutes) if horizon_minutes is not None else state.config.forecast.horizon))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/incidents", response_model=list[Incident])
def list_incidents(
    status: list[str] | None = Query(default=None, description="Filtrer par statut : suspect, open, stale, closed"),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[Incident]:
    return state.storage.list_incidents(statuses=status, limit=limit)


@app.get("/incidents/{incident_id}", response_model=Incident)
def get_incident(incident_id: str) -> Incident:
    incident = state.storage.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"Incident inconnu : {incident_id}")
    return incident


@app.get("/incidents/{incident_id}/alerts", response_model=list[Alert])
def incident_alerts(incident_id: str, limit: int = Query(default=100, ge=1, le=500)) -> list[Alert]:
    """
    Chronologie des alertes d'un incident (§27) : SUSPECT → OUVERT → MISE À JOUR
    → RÉCUPÉRATION → FERMÉ (ou STALE), de la plus récente à la plus ancienne.
    """
    if state.storage.get_incident(incident_id) is None:
        raise HTTPException(status_code=404, detail=f"Incident inconnu : {incident_id}")
    return state.storage.get_alerts(incident_id=incident_id, limit=limit)


@app.get("/outputs/{output_id}/alerts", response_model=list[Alert])
def output_alerts(
    output_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[Alert]:
    _ensure_known_output(output_id)
    start, end = _parse_window(since, until, timedelta(days=1))
    return state.storage.get_alerts(output_id=output_id, start=start, end=end, limit=limit)


@app.get("/monitoring/status", response_model=MonitoringStatus)
def monitoring_status() -> MonitoringStatus:
    return MonitoringStatus(
        running=state.running,
        calibrated=state.calibrated,
        simulated_now=state.now,
        interval_seconds=state.source.get_interval().total_seconds(),
        seconds_per_tick=state.seconds_per_tick,
        outputs=state.source.list_outputs(),
    )


@app.post("/monitoring/start", response_model=MonitoringStatus)
def start_monitoring() -> MonitoringStatus:
    state.start()
    return monitoring_status()


@app.post("/monitoring/stop", response_model=MonitoringStatus)
def stop_monitoring() -> MonitoringStatus:
    state.stop()
    return monitoring_status()


@app.get("/config", response_model=AppConfig)
def get_config() -> AppConfig:
    return state.config


@app.put("/config", response_model=AppConfig)
def update_config(update: ConfigUpdate) -> AppConfig:
    """
    Met à jour à chaud le sous-ensemble modifiable de la configuration
    (§35). Reconstruit un IncidentConfig complet pour que les validateurs
    (§21 : close_score < warning_score < open_score) s'appliquent avant
    toute prise d'effet — jamais d'affectation directe d'attribut, qui
    contournerait ces règles.
    """
    current = state.config.incident
    merged = current.model_dump()
    if update.open_score is not None:
        merged["open_score"] = update.open_score
    if update.close_score is not None:
        merged["close_score"] = update.close_score
    if update.warning_score is not None:
        merged["warning_score"] = update.warning_score
    if update.minimum_duration_seconds is not None:
        merged["minimum_duration"] = timedelta(seconds=update.minimum_duration_seconds)
    if update.recovery_duration_seconds is not None:
        merged["recovery_duration"] = timedelta(seconds=update.recovery_duration_seconds)

    try:
        new_incident_config = IncidentConfig.model_validate(merged)
    except Exception as exc:  # ValidationError Pydantic
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    new_critical_score = update.critical_score if update.critical_score is not None else state.config.alerts.critical_score

    state.config.incident = new_incident_config
    state.config.alerts.critical_score = new_critical_score
    state.loop.incident_manager.update_config(new_incident_config, critical_score=new_critical_score)

    return state.config
