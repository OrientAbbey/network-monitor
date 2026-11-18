"""
Stockage : tables séparées (mesures, prévisions, anomalies,
incidents, alertes) pour éviter qu'un incident ne duplique les observations
brutes.

Sécurité : toutes les requêtes passent par l'ORM SQLAlchemy (paramétrées
nativement, jamais de SQL construit par concaténation de chaînes) ; la
sérialisation des `evidence` utilise JSON via Pydantic, jamais `pickle`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    String,
    create_engine,
    event,
    func,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from network_monitor.analysis.availability import SampleStats
from network_monitor.app.models import (
    AnomalyResult,
    ForecastModelName,
    ForecastResult,
    Incident,
    IncidentEvidence,
    IncidentStatus,
    IncidentType,
    IncidentVerdict,
    MaintenanceWindow,
    Severity,
    TrafficSample,
)
from network_monitor.incidents.manager import Alert
from network_monitor.storage.archive import ParquetArchive


class Base(DeclarativeBase):
    pass


@dataclass
class Notification:
    """Un envoi à faire (ou fait) : une alerte vers un canal. Voir `NotificationRow`."""

    id: str
    channel: str
    alert: dict[str, Any]
    created_at: datetime
    next_attempt_at: datetime
    status: str = "pending"
    attempts: int = 0
    sent_at: datetime | None = None
    last_error: str | None = None
    pending_targets: list[str] | None = None


def _to_utc(dt: datetime) -> datetime:
    """Normalise en UTC avant écriture (SQLite ne conserve pas le fuseau horaire)."""
    return dt.astimezone(timezone.utc)


def _opt_utc(dt: datetime | None) -> datetime | None:
    return None if dt is None else _to_utc(dt)


def _as_utc(dt: datetime) -> datetime:
    """Ré-attache le fuseau UTC à la lecture (perdu par le driver SQLite : toute date doit être timezone-aware)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _opt_as_utc(dt: datetime | None) -> datetime | None:
    return None if dt is None else _as_utc(dt)


_TS = DateTime(timezone=True)


class RawMeasurementRow(Base):
    __tablename__ = "raw_measurements"
    output_id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(_TS, primary_key=True)
    traffic_mbps: Mapped[float] = mapped_column(Float)
    loss_percent: Mapped[float] = mapped_column(Float)


class ForecastRow(Base):
    __tablename__ = "forecasts"
    output_id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(_TS, primary_key=True)
    model_name: Mapped[str] = mapped_column(String)
    predicted_traffic_mbps: Mapped[float] = mapped_column(Float)
    lower_bound_mbps: Mapped[float] = mapped_column(Float)
    upper_bound_mbps: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)


class AnomalyRow(Base):
    __tablename__ = "anomalies"
    output_id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(_TS, primary_key=True)
    traffic_anomaly_score: Mapped[float] = mapped_column(Float)
    loss_anomaly_score: Mapped[float] = mapped_column(Float)
    persistence_score: Mapped[float] = mapped_column(Float)
    joint_anomaly_score: Mapped[float] = mapped_column(Float)
    data_gap: Mapped[bool] = mapped_column(Boolean)
    traffic_deviation_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss_deviation_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    traffic_out_of_interval: Mapped[bool] = mapped_column(Boolean, default=False)
    isolation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    features: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)


class IncidentRow(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    output_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String)
    incident_type: Mapped[str] = mapped_column(String)
    suspected_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(_TS, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    score: Mapped[float] = mapped_column(Float)
    peak_traffic_deviation_percent: Mapped[float] = mapped_column(Float)
    peak_loss_percent: Mapped[float] = mapped_column(Float)
    description: Mapped[str] = mapped_column(String)
    maintenance: Mapped[bool] = mapped_column(Boolean, default=False)
    root_cause_incident_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    inhibited: Mapped[bool] = mapped_column(Boolean, default=False)
    # Les preuves sont propres à l'incident (résumés, pas les mesures brutes) :
    # un JSON compact est plus simple qu'une table de jointure pour
    # un volume par incident qui reste faible (plafonné, cf. IncidentManager).
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON)

    __table_args__ = (Index("ix_incidents_output_status", "output_id", "status"),)


class AlertRow(Base):
    """Une ligne par alerte émise : trace d'audit légère, découplée du
    cycle de vie de l'incident (contrairement à IncidentRow, jamais mise à
    jour en place — chaque alerte est un fait ponctuel)."""

    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String, primary_key=True)  # incident_id + horodatage + event
    incident_id: Mapped[str] = mapped_column(String, index=True)
    output_id: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String)
    event: Mapped[str] = mapped_column(String)
    timestamp: Mapped[datetime] = mapped_column(_TS, index=True)
    message: Mapped[str] = mapped_column(String)
    incident_type: Mapped[str] = mapped_column(String)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    traffic_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_traffic_mbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    traffic_deviation_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    loss_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_loss_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    suppressed: Mapped[str | None] = mapped_column(String, nullable=True)
    root_cause_incident_id: Mapped[str | None] = mapped_column(String, nullable=True)
    impacted_outputs: Mapped[list[str]] = mapped_column(JSON, default=list)


class IncidentFeedbackRow(Base):
    """Jugement d'un opérateur, **hors** de la table des incidents : le moteur réécrit la ligne d'un incident
    à chaque pas, un verdict stocké dans cette ligne serait écrasé."""

    __tablename__ = "incident_feedback"
    incident_id: Mapped[str] = mapped_column(String, primary_key=True)
    verdict: Mapped[str] = mapped_column(String)
    by: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(_TS)
    note: Mapped[str | None] = mapped_column(String, nullable=True)


class IncidentAckRow(Base):
    """Prise en charge d'un incident par un opérateur, **hors** de la table des incidents (même raison que les
    verdicts : le moteur réécrit la ligne de l'incident à chaque pas). Table nouvelle : `create_all` la crée dans une
    base existante sans migration."""

    __tablename__ = "incident_ack"
    incident_id: Mapped[str] = mapped_column(String, primary_key=True)
    by: Mapped[str] = mapped_column(String)
    at: Mapped[datetime] = mapped_column(_TS)


class MaintenanceRow(Base):
    __tablename__ = "maintenance_windows"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    output_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    start: Mapped[datetime] = mapped_column(_TS, index=True)
    end: Mapped[datetime] = mapped_column(_TS, index=True)
    reason: Mapped[str] = mapped_column(String)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(_TS)
    cancelled_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)


class NotificationRow(Base):
    """File d'envoi (« outbox ») : une ligne par alerte et par canal, écrite dans la même
    transaction que l'alerte. Un envoi qui échoue est retenté ; un redémarrage ne perd rien."""

    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    channel: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(_TS)
    next_attempt_at: Mapped[datetime] = mapped_column(_TS, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    pending_targets: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    """Destinataires restant à servir (SMS de groupe : seuls ceux qui ont échoué sont retentés)."""
    alert: Mapped[dict[str, Any]] = mapped_column(JSON)


class Repository(ABC):
    """Abstraction de stockage, non couplée au Mock ni aux algorithmes."""

    @abstractmethod
    def save_samples(self, samples: list[TrafficSample]) -> None: ...

    @abstractmethod
    def save_forecasts(self, forecasts: list[ForecastResult]) -> None: ...

    @abstractmethod
    def save_anomalies(self, anomalies: list[AnomalyResult]) -> None: ...

    @abstractmethod
    def upsert_incident(self, incident: Incident) -> None: ...

    @abstractmethod
    def save_tick(
        self,
        samples: list[TrafficSample],
        forecasts: list[ForecastResult],
        anomalies: list[AnomalyResult],
        incidents: list[Incident],
        alerts: list[Alert],
        notifications: list[Notification] | None = None,
    ) -> None:
        """Écrit tous les résultats d'un tick en une seule transaction : un
        seul commit (donc un seul fsync) au lieu d'un par type de donnée. Les
        notifications y sont incluses : une alerte et son envoi ne se séparent jamais."""
        ...

    @abstractmethod
    def due_notifications(self, channel: str, now: datetime, limit: int = 200) -> list[Notification]:
        """Envois en attente d'un canal, dont l'heure de (re)tentative est venue."""
        ...

    @abstractmethod
    def update_notifications(self, items: list[Notification]) -> None: ...

    @abstractmethod
    def list_notifications(self, status: str | None = None, limit: int = 100) -> list[Notification]: ...

    @abstractmethod
    def save_alerts(self, alerts: list[Alert]) -> None: ...

    @abstractmethod
    def list_alerts(self, output_id: str | None = None, limit: int = 100, notable_only: bool = False) -> list[Alert]: ...

    @abstractmethod
    def get_history(self, output_id: str, start: datetime, end: datetime) -> list[TrafficSample]: ...

    @abstractmethod
    def get_anomalies(self, output_id: str, start: datetime, end: datetime) -> list[AnomalyResult]: ...

    @abstractmethod
    def get_forecasts(self, output_id: str, start: datetime, end: datetime) -> list[ForecastResult]: ...

    @abstractmethod
    def list_active_incidents(self) -> list[Incident]: ...

    @abstractmethod
    def list_incidents(self, statuses: list[str] | None = None, limit: int = 50) -> list[Incident]: ...

    @abstractmethod
    def get_incident(self, incident_id: str) -> Incident | None: ...

    @abstractmethod
    def set_feedback(
        self, incident_id: str, verdict: IncidentVerdict | None, by: str, at: datetime, note: str | None = None
    ) -> None:
        """Enregistre (ou, avec `verdict=None`, retire) le jugement d'un opérateur sur un incident."""
        ...

    @abstractmethod
    def set_ack(self, incident_id: str, by: str | None, at: datetime) -> None:
        """Enregistre (ou, avec `by=None`, retire) la prise en charge d'un incident par un opérateur."""
        ...

    @abstractmethod
    def add_maintenance(self, window: MaintenanceWindow) -> None: ...

    @abstractmethod
    def cancel_maintenance(self, window_id: str, at: datetime) -> bool:
        """Annule une fenêtre (conservée pour l'audit). False si elle n'existe pas ou l'était déjà."""
        ...

    @abstractmethod
    def list_maintenance(
        self, start: datetime, end: datetime, include_cancelled: bool = False
    ) -> list[MaintenanceWindow]:
        """Fenêtres qui chevauchent [start, end[."""
        ...

    @abstractmethod
    def list_incidents_between(self, start: datetime, end: datetime) -> list[Incident]:
        """Incidents confirmés qui chevauchent [start, end[ (début rétro-daté avant `end`, dernier pas vu après `start`)."""
        ...

    @abstractmethod
    def sample_stats(self, start: datetime, end: datetime) -> dict[str, SampleStats]:
        """Nombre de mesures brutes conservées et date de la première, par sortie, sur [start, end[."""
        ...

    @abstractmethod
    def purge_older_than(self, cutoff: datetime) -> None:
        """Supprime les mesures/prévisions/anomalies antérieures à `cutoff` (les mesures sont
        d'abord archivées si une archive Parquet est configurée).
        Les incidents et les alertes ne sont jamais purgés ici : ce sont des
        événements, pas des séries qui croissent indéfiniment au même rythme."""
        ...

    @abstractmethod
    def ping(self) -> bool:
        """La base répond-elle ? (santé du service : jamais d'exception, un booléen)."""
        ...

    @abstractmethod
    def count_notifications(self) -> dict[tuple[str, str], int]:
        """Nombre de notifications par (canal, statut), pour la supervision."""
        ...

    @abstractmethod
    def dispose(self) -> None:
        """Ferme proprement les connexions sous-jacentes (tests, arrêt propre)."""
        ...


class SqlAlchemyRepository(Repository):
    """
    Implémentation par défaut, sur SQLite par un fichier local (gratuit,
    sans dépendance externe — adapté à un environnement contraint).
    """

    def __init__(self, url: str = "sqlite:///network_monitor.db", archive: ParquetArchive | None = None) -> None:
        self._archive = archive
        # `check_same_thread=False` : l'API fait tourner la boucle de monitoring
        # dans un thread dédié pendant que les requêtes HTTP arrivent sur un
        # autre thread. Chaque appel garde une session courte (`with self._session()`),
        # ce qui reste sûr avec SQLite tant qu'aucune session n'est partagée
        # entre threads — seul l'engine (le pool de connexions) l'est.
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self._engine = create_engine(url, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            # WAL : les lectures (dashboard, API) ne sont plus bloquées par les
            # écritures de la boucle de monitoring. synchronous=NORMAL est sûr
            # en WAL (seule une coupure du système, pas juste du process, peut
            # perdre les toutes dernières transactions — acceptable ici).
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, _):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine, future=True)

    def _session(self) -> Session:
        return self._session_factory()

    def dispose(self) -> None:
        self._engine.dispose()

    # -- Écritures : chaque `_merge_*` travaille dans la session de l'appelant,
    # pour que `save_tick` regroupe tout en une transaction et que les
    # méthodes unitaires (`save_samples`, ...) restent de simples enveloppes.

    @staticmethod
    def _merge_samples(session: Session, samples: list[TrafficSample]) -> None:
        for s in samples:
            session.merge(
                RawMeasurementRow(
                    output_id=s.output_id,
                    timestamp=_to_utc(s.timestamp),
                    traffic_mbps=s.traffic_mbps,
                    loss_percent=s.loss_percent,
                )
            )

    @staticmethod
    def _merge_forecasts(session: Session, forecasts: list[ForecastResult]) -> None:
        for f in forecasts:
            session.merge(
                ForecastRow(
                    output_id=f.output_id,
                    timestamp=_to_utc(f.timestamp),
                    model_name=f.model_name.value,
                    predicted_traffic_mbps=f.predicted_traffic_mbps,
                    lower_bound_mbps=f.lower_bound_mbps,
                    upper_bound_mbps=f.upper_bound_mbps,
                    confidence=f.confidence,
                )
            )

    @staticmethod
    def _merge_anomalies(session: Session, anomalies: list[AnomalyResult]) -> None:
        for a in anomalies:
            session.merge(
                AnomalyRow(
                    output_id=a.output_id,
                    timestamp=_to_utc(a.timestamp),
                    traffic_anomaly_score=a.traffic_anomaly_score,
                    loss_anomaly_score=a.loss_anomaly_score,
                    persistence_score=a.persistence_score,
                    joint_anomaly_score=a.joint_anomaly_score,
                    data_gap=a.data_gap,
                    traffic_deviation_percent=a.traffic_deviation_percent,
                    loss_deviation_points=a.loss_deviation_points,
                    traffic_out_of_interval=a.traffic_out_of_interval,
                    isolation_score=a.isolation_score,
                    features=a.features,
                )
            )

    @staticmethod
    def _merge_incidents(session: Session, incidents: list[Incident]) -> None:
        for incident in incidents:
            session.merge(
                IncidentRow(
                    id=incident.id,
                    output_id=incident.output_id,
                    status=incident.status.value,
                    severity=incident.severity.value,
                    incident_type=incident.incident_type.value,
                    suspected_at=_opt_utc(incident.suspected_at),
                    opened_at=_opt_utc(incident.opened_at),
                    last_seen_at=_to_utc(incident.last_seen_at),
                    closed_at=_opt_utc(incident.closed_at),
                    started_at=_opt_utc(incident.started_at),
                    ended_at=_opt_utc(incident.ended_at),
                    score=incident.score,
                    peak_traffic_deviation_percent=incident.peak_traffic_deviation_percent,
                    peak_loss_percent=incident.peak_loss_percent,
                    description=incident.description,
                    maintenance=incident.maintenance,
                    root_cause_incident_id=incident.root_cause_incident_id,
                    inhibited=incident.inhibited,
                    # `mode="json"` produit des types déjà sérialisables (dates en ISO 8601).
                    evidence=[e.model_dump(mode="json") for e in incident.evidence],
                )
            )

    @staticmethod
    def _merge_alerts(session: Session, alerts: list[Alert]) -> None:
        for a in alerts:
            session.merge(
                AlertRow(
                    id=f"{a.incident_id}:{a.timestamp.isoformat()}:{a.event}",
                    incident_id=a.incident_id,
                    output_id=a.output_id,
                    severity=a.severity.value,
                    event=a.event,
                    timestamp=_to_utc(a.timestamp),
                    message=a.message,
                    incident_type=a.incident_type.value,
                    duration_seconds=a.duration_seconds,
                    traffic_mbps=a.traffic_mbps,
                    expected_traffic_mbps=a.expected_traffic_mbps,
                    traffic_deviation_percent=a.traffic_deviation_percent,
                    loss_percent=a.loss_percent,
                    expected_loss_percent=a.expected_loss_percent,
                    started_at=_opt_utc(a.started_at),
                    ended_at=_opt_utc(a.ended_at),
                    confirmed=a.confirmed,
                    suppressed=a.suppressed,
                    root_cause_incident_id=a.root_cause_incident_id,
                    impacted_outputs=a.impacted_outputs,
                )
            )

    @staticmethod
    def _merge_notifications(session: Session, items: list[Notification]) -> None:
        for n in items:
            session.merge(
                NotificationRow(
                    id=n.id, channel=n.channel, status=n.status, attempts=n.attempts,
                    created_at=_to_utc(n.created_at), next_attempt_at=_to_utc(n.next_attempt_at),
                    sent_at=_opt_utc(n.sent_at), last_error=n.last_error,
                    pending_targets=n.pending_targets, alert=n.alert,
                )
            )

    @staticmethod
    def _notification(r: NotificationRow) -> Notification:
        return Notification(
            id=r.id, channel=r.channel, alert=r.alert, created_at=_as_utc(r.created_at),
            next_attempt_at=_as_utc(r.next_attempt_at), status=r.status, attempts=r.attempts,
            sent_at=_opt_as_utc(r.sent_at), last_error=r.last_error, pending_targets=r.pending_targets,
        )

    def due_notifications(self, channel: str, now: datetime, limit: int = 200) -> list[Notification]:
        with self._session() as session:
            rows = (
                session.query(NotificationRow)
                .filter(NotificationRow.channel == channel, NotificationRow.status == "pending",
                        NotificationRow.next_attempt_at <= _to_utc(now))
                .order_by(NotificationRow.created_at)
                .limit(limit)
                .all()
            )
            return [self._notification(r) for r in rows]

    def update_notifications(self, items: list[Notification]) -> None:
        with self._session() as session:
            self._merge_notifications(session, items)
            session.commit()

    def ping(self) -> bool:
        try:
            with self._session() as session:
                session.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def count_notifications(self) -> dict[tuple[str, str], int]:
        with self._session() as session:
            rows = session.query(NotificationRow.channel, NotificationRow.status, func.count()).group_by(
                NotificationRow.channel, NotificationRow.status
            )
            return {(channel, status): n for channel, status, n in rows}

    def list_notifications(self, status: str | None = None, limit: int = 100) -> list[Notification]:
        with self._session() as session:
            query = session.query(NotificationRow)
            if status:
                query = query.filter(NotificationRow.status == status)
            return [self._notification(r) for r in query.order_by(NotificationRow.created_at.desc()).limit(limit).all()]

    def save_samples(self, samples: list[TrafficSample]) -> None:
        with self._session() as session:
            self._merge_samples(session, samples)
            session.commit()

    def save_forecasts(self, forecasts: list[ForecastResult]) -> None:
        with self._session() as session:
            self._merge_forecasts(session, forecasts)
            session.commit()

    def save_anomalies(self, anomalies: list[AnomalyResult]) -> None:
        with self._session() as session:
            self._merge_anomalies(session, anomalies)
            session.commit()

    def upsert_incident(self, incident: Incident) -> None:
        with self._session() as session:
            self._merge_incidents(session, [incident])
            session.commit()

    def save_alerts(self, alerts: list[Alert]) -> None:
        with self._session() as session:
            self._merge_alerts(session, alerts)
            session.commit()

    def save_tick(
        self,
        samples: list[TrafficSample],
        forecasts: list[ForecastResult],
        anomalies: list[AnomalyResult],
        incidents: list[Incident],
        alerts: list[Alert],
        notifications: list[Notification] | None = None,
    ) -> None:
        with self._session() as session:
            self._merge_samples(session, samples)
            self._merge_forecasts(session, forecasts)
            self._merge_anomalies(session, anomalies)
            self._merge_incidents(session, incidents)
            self._merge_alerts(session, alerts)
            self._merge_notifications(session, notifications or [])
            session.commit()

    # -- Lectures ------------------------------------------------------------

    def list_alerts(self, output_id: str | None = None, limit: int = 100, notable_only: bool = False) -> list[Alert]:
        with self._session() as session:
            query = session.query(AlertRow)
            if notable_only:
                query = query.filter(AlertRow.confirmed.is_(True), AlertRow.event.not_in(("suspect", "recovering")))
            if output_id is not None:
                query = query.filter(AlertRow.output_id == output_id)
            rows = query.order_by(AlertRow.timestamp.desc()).limit(limit).all()
            return [
                Alert(
                    incident_id=r.incident_id,
                    output_id=r.output_id,
                    severity=Severity(r.severity),
                    event=r.event,
                    timestamp=_as_utc(r.timestamp),
                    message=r.message,
                    incident_type=IncidentType(r.incident_type),
                    duration_seconds=r.duration_seconds,
                    traffic_mbps=r.traffic_mbps,
                    expected_traffic_mbps=r.expected_traffic_mbps,
                    traffic_deviation_percent=r.traffic_deviation_percent,
                    loss_percent=r.loss_percent,
                    expected_loss_percent=r.expected_loss_percent,
                    started_at=_opt_as_utc(r.started_at),
                    ended_at=_opt_as_utc(r.ended_at),
                    confirmed=r.confirmed,
                    suppressed=r.suppressed,
                    root_cause_incident_id=r.root_cause_incident_id,
                    impacted_outputs=list(r.impacted_outputs or []),
                )
                for r in rows
            ]

    def get_history(self, output_id: str, start: datetime, end: datetime) -> list[TrafficSample]:
        with self._session() as session:
            rows = (
                session.query(RawMeasurementRow)
                .filter(
                    RawMeasurementRow.output_id == output_id,
                    RawMeasurementRow.timestamp >= _to_utc(start),
                    RawMeasurementRow.timestamp < _to_utc(end),
                )
                .order_by(RawMeasurementRow.timestamp)
                .all()
            )
            return [
                TrafficSample(
                    timestamp=_as_utc(r.timestamp),
                    output_id=r.output_id,
                    traffic_mbps=r.traffic_mbps,
                    loss_percent=r.loss_percent,
                )
                for r in rows
            ]

    def get_anomalies(self, output_id: str, start: datetime, end: datetime) -> list[AnomalyResult]:
        with self._session() as session:
            rows = (
                session.query(AnomalyRow)
                .filter(
                    AnomalyRow.output_id == output_id,
                    AnomalyRow.timestamp >= _to_utc(start),
                    AnomalyRow.timestamp < _to_utc(end),
                )
                .order_by(AnomalyRow.timestamp)
                .all()
            )
            return [
                AnomalyResult(
                    output_id=r.output_id,
                    timestamp=_as_utc(r.timestamp),
                    traffic_anomaly_score=r.traffic_anomaly_score,
                    loss_anomaly_score=r.loss_anomaly_score,
                    persistence_score=r.persistence_score,
                    joint_anomaly_score=r.joint_anomaly_score,
                    data_gap=r.data_gap,
                    traffic_deviation_percent=r.traffic_deviation_percent,
                    loss_deviation_points=r.loss_deviation_points,
                    traffic_out_of_interval=r.traffic_out_of_interval,
                    isolation_score=r.isolation_score,
                    features=r.features or {},
                )
                for r in rows
            ]

    def get_forecasts(self, output_id: str, start: datetime, end: datetime) -> list[ForecastResult]:
        with self._session() as session:
            rows = (
                session.query(ForecastRow)
                .filter(
                    ForecastRow.output_id == output_id,
                    ForecastRow.timestamp >= _to_utc(start),
                    ForecastRow.timestamp < _to_utc(end),
                )
                .order_by(ForecastRow.timestamp)
                .all()
            )
            return [
                ForecastResult(
                    output_id=r.output_id,
                    timestamp=_as_utc(r.timestamp),
                    predicted_traffic_mbps=r.predicted_traffic_mbps,
                    lower_bound_mbps=r.lower_bound_mbps,
                    upper_bound_mbps=r.upper_bound_mbps,
                    confidence=r.confidence,
                    model_name=ForecastModelName(r.model_name),
                )
                for r in rows
            ]

    def list_active_incidents(self) -> list[Incident]:
        active_statuses = ["suspect", "open", "stale"]
        with self._session() as session:
            rows = session.query(IncidentRow).filter(IncidentRow.status.in_(active_statuses)).all()
            return self._with_operator_state(session, [_incident_from_row(r) for r in rows])

    def list_incidents(self, statuses: list[str] | None = None, limit: int = 50) -> list[Incident]:
        with self._session() as session:
            query = session.query(IncidentRow)
            if statuses:
                query = query.filter(IncidentRow.status.in_(statuses))
            rows = query.order_by(IncidentRow.last_seen_at.desc()).limit(limit).all()
            return self._with_operator_state(session, [_incident_from_row(r) for r in rows])

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._session() as session:
            row = session.query(IncidentRow).filter(IncidentRow.id == incident_id).one_or_none()
            return self._with_operator_state(session, [_incident_from_row(row)])[0] if row is not None else None

    @staticmethod
    def _with_operator_state(session: Session, incidents: list[Incident]) -> list[Incident]:
        """Joint aux incidents lus ce que les opérateurs et l'escalade y ont ajouté, hors de leur table : le jugement,
        la prise en charge, et l'instant de l'alerte `escalated` s'il y en a eu une."""
        if not incidents:
            return incidents
        ids = [i.id for i in incidents]
        feedback = {r.incident_id: r for r in session.query(IncidentFeedbackRow).filter(IncidentFeedbackRow.incident_id.in_(ids))}
        acks = {r.incident_id: r for r in session.query(IncidentAckRow).filter(IncidentAckRow.incident_id.in_(ids))}
        escalated = dict(
            session.query(AlertRow.incident_id, func.min(AlertRow.timestamp))
            .filter(AlertRow.incident_id.in_(ids), AlertRow.event == "escalated")
            .group_by(AlertRow.incident_id)
        )
        for incident in incidents:
            fb = feedback.get(incident.id)
            if fb is not None:
                incident.verdict = IncidentVerdict(fb.verdict)
                incident.verdict_by, incident.verdict_at, incident.verdict_note = fb.by, _as_utc(fb.at), fb.note
            ack = acks.get(incident.id)
            if ack is not None:
                incident.acknowledged_by, incident.acknowledged_at = ack.by, _as_utc(ack.at)
            if incident.id in escalated:
                incident.escalated_at = _as_utc(escalated[incident.id])
        return incidents

    def set_feedback(
        self, incident_id: str, verdict: IncidentVerdict | None, by: str, at: datetime, note: str | None = None
    ) -> None:
        with self._session() as session:
            if verdict is None:
                session.query(IncidentFeedbackRow).filter(IncidentFeedbackRow.incident_id == incident_id).delete()
            else:
                session.merge(
                    IncidentFeedbackRow(incident_id=incident_id, verdict=verdict.value, by=by, at=_to_utc(at), note=note)
                )
            session.commit()

    def set_ack(self, incident_id: str, by: str | None, at: datetime) -> None:
        with self._session() as session:
            if by is None:
                session.query(IncidentAckRow).filter(IncidentAckRow.incident_id == incident_id).delete()
            else:
                session.merge(IncidentAckRow(incident_id=incident_id, by=by, at=_to_utc(at)))
            session.commit()

    @staticmethod
    def _window(r: MaintenanceRow) -> MaintenanceWindow:
        return MaintenanceWindow(
            id=r.id, output_id=r.output_id, start=_as_utc(r.start), end=_as_utc(r.end), reason=r.reason,
            created_by=r.created_by, created_at=_as_utc(r.created_at), cancelled_at=_opt_as_utc(r.cancelled_at),
        )

    def add_maintenance(self, window: MaintenanceWindow) -> None:
        with self._session() as session:
            session.merge(
                MaintenanceRow(
                    id=window.id, output_id=window.output_id, start=_to_utc(window.start), end=_to_utc(window.end),
                    reason=window.reason, created_by=window.created_by, created_at=_to_utc(window.created_at),
                    cancelled_at=_opt_utc(window.cancelled_at),
                )
            )
            session.commit()

    def cancel_maintenance(self, window_id: str, at: datetime) -> bool:
        with self._session() as session:
            row = session.query(MaintenanceRow).filter(MaintenanceRow.id == window_id).one_or_none()
            if row is None or row.cancelled_at is not None:
                return False
            row.cancelled_at = _to_utc(at)
            session.commit()
            return True

    def list_maintenance(
        self, start: datetime, end: datetime, include_cancelled: bool = False
    ) -> list[MaintenanceWindow]:
        with self._session() as session:
            query = session.query(MaintenanceRow).filter(
                MaintenanceRow.start < _to_utc(end), MaintenanceRow.end > _to_utc(start)
            )
            if not include_cancelled:
                query = query.filter(MaintenanceRow.cancelled_at.is_(None))
            return [self._window(r) for r in query.order_by(MaintenanceRow.start).all()]

    def list_incidents_between(self, start: datetime, end: datetime) -> list[Incident]:
        with self._session() as session:
            rows = (
                session.query(IncidentRow)
                .filter(
                    IncidentRow.opened_at.is_not(None),
                    IncidentRow.last_seen_at >= _to_utc(start),
                    func.coalesce(IncidentRow.started_at, IncidentRow.opened_at) < _to_utc(end),
                )
                .order_by(IncidentRow.opened_at)
                .all()
            )
            return self._with_operator_state(session, [_incident_from_row(r) for r in rows])

    def sample_stats(self, start: datetime, end: datetime) -> dict[str, SampleStats]:
        with self._session() as session:
            rows = (
                session.query(RawMeasurementRow.output_id, func.count(), func.min(RawMeasurementRow.timestamp))
                .filter(RawMeasurementRow.timestamp >= _to_utc(start), RawMeasurementRow.timestamp < _to_utc(end))
                .group_by(RawMeasurementRow.output_id)
                .all()
            )
            return {output_id: SampleStats(int(n), _as_utc(first)) for output_id, n, first in rows}

    def purge_older_than(self, cutoff: datetime) -> None:
        cutoff_utc = _to_utc(cutoff)
        if self._archive is not None:
            self._archive_raw_before(cutoff_utc)
        with self._session() as session:
            session.query(RawMeasurementRow).filter(RawMeasurementRow.timestamp < cutoff_utc).delete(synchronize_session=False)
            session.query(ForecastRow).filter(ForecastRow.timestamp < cutoff_utc).delete(synchronize_session=False)
            session.query(AnomalyRow).filter(AnomalyRow.timestamp < cutoff_utc).delete(synchronize_session=False)
            session.commit()

    def _archive_raw_before(self, cutoff: datetime) -> None:
        """Archive puis supprime les mesures brutes antérieures à `cutoff`, un jour à la fois
        (la mémoire reste bornée à une journée de mesures, quel que soit le retard accumulé).
        Une archive qui échoue interrompt la purge : mieux vaut garder la donnée que la perdre."""
        assert self._archive is not None
        while True:
            with self._session() as session:
                first = session.query(func.min(RawMeasurementRow.timestamp)).scalar()
                if first is None or _as_utc(first) >= cutoff:
                    return
                day_start = _as_utc(first).replace(hour=0, minute=0, second=0, microsecond=0)
                day_end = min(day_start + timedelta(days=1), cutoff)
                rows = (
                    session.query(RawMeasurementRow)
                    .filter(RawMeasurementRow.timestamp >= day_start, RawMeasurementRow.timestamp < day_end)
                    .all()
                )
                self._archive.write(
                    TrafficSample(
                        timestamp=_as_utc(r.timestamp), output_id=r.output_id,
                        traffic_mbps=r.traffic_mbps, loss_percent=r.loss_percent,
                    )
                    for r in rows
                )
                session.query(RawMeasurementRow).filter(
                    RawMeasurementRow.timestamp >= day_start, RawMeasurementRow.timestamp < day_end
                ).delete(synchronize_session=False)
                session.commit()


def _incident_from_row(row: IncidentRow) -> Incident:
    return Incident(
        id=row.id,
        output_id=row.output_id,
        status=IncidentStatus(row.status),
        severity=Severity(row.severity),
        incident_type=IncidentType(row.incident_type),
        suspected_at=_opt_as_utc(row.suspected_at),
        opened_at=_opt_as_utc(row.opened_at),
        last_seen_at=_as_utc(row.last_seen_at),
        closed_at=_opt_as_utc(row.closed_at),
        started_at=_opt_as_utc(row.started_at),
        ended_at=_opt_as_utc(row.ended_at),
        score=row.score,
        peak_traffic_deviation_percent=row.peak_traffic_deviation_percent,
        peak_loss_percent=row.peak_loss_percent,
        description=row.description,
        maintenance=row.maintenance,
        root_cause_incident_id=row.root_cause_incident_id,
        inhibited=row.inhibited,
        # `model_dump(mode="json")` sérialise les timestamps en ISO 8601 UTC ;
        # Pydantic les re-parse directement en datetime timezone-aware.
        evidence=[IncidentEvidence.model_validate(e) for e in row.evidence],
    )
