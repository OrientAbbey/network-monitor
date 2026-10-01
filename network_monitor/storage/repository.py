"""
Stockage (§29) : tables séparées (mesures, prévisions, anomalies,
incidents, alertes) pour éviter qu'un incident ne duplique les observations
brutes.

Sécurité : toutes les requêtes passent par l'ORM SQLAlchemy (paramétrées
nativement, jamais de SQL construit par concaténation de chaînes) ; la
sérialisation des `evidence` utilise JSON via Pydantic, jamais `pickle`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from network_monitor.app.models import (
    AnomalyResult,
    ForecastModelName,
    ForecastResult,
    Incident,
    IncidentEvidence,
    IncidentStatus,
    IncidentType,
    Severity,
    TrafficSample,
)
from network_monitor.incidents.manager import Alert


class Base(DeclarativeBase):
    pass


def _to_utc(dt: datetime) -> datetime:
    """Normalise en UTC avant écriture (SQLite ne conserve pas le fuseau horaire)."""
    return dt.astimezone(timezone.utc)


def _opt_utc(dt: datetime | None) -> datetime | None:
    return None if dt is None else _to_utc(dt)


def _as_utc(dt: datetime) -> datetime:
    """Ré-attache le fuseau UTC à la lecture (perdu par le driver SQLite, §3 exige timezone-aware)."""
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
    features: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)  # §9


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
    score: Mapped[float] = mapped_column(Float)
    peak_traffic_deviation_percent: Mapped[float] = mapped_column(Float)
    peak_loss_percent: Mapped[float] = mapped_column(Float)
    description: Mapped[str] = mapped_column(String)
    # Les preuves sont propres à l'incident (résumés, pas les mesures brutes,
    # §29) : un JSON compact est plus simple qu'une table de jointure pour
    # un volume par incident qui reste faible (plafonné, cf. IncidentManager).
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON)

    __table_args__ = (Index("ix_incidents_output_status", "output_id", "status"),)


class AlertRow(Base):
    """Une ligne par alerte émise (§27) : trace d'audit légère, découplée du
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


class Repository(ABC):
    """Abstraction de stockage (§29), non couplée au Mock ni aux algorithmes."""

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
    ) -> None:
        """Écrit tous les résultats d'un tick en une seule transaction : un
        seul commit (donc un seul fsync) au lieu d'un par type de donnée."""
        ...

    @abstractmethod
    def save_alerts(self, alerts: list[Alert]) -> None: ...

    @abstractmethod
    def list_alerts(self, output_id: str | None = None, limit: int = 100) -> list[Alert]: ...

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
    def purge_older_than(self, cutoff: datetime) -> None:
        """Supprime les mesures/prévisions/anomalies antérieures à `cutoff`.
        Les incidents et les alertes ne sont jamais purgés ici : ce sont des
        événements, pas des séries qui croissent indéfiniment au même rythme."""
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

    def __init__(self, url: str = "sqlite:///network_monitor.db") -> None:
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
                    score=incident.score,
                    peak_traffic_deviation_percent=incident.peak_traffic_deviation_percent,
                    peak_loss_percent=incident.peak_loss_percent,
                    description=incident.description,
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
                )
            )

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
    ) -> None:
        with self._session() as session:
            self._merge_samples(session, samples)
            self._merge_forecasts(session, forecasts)
            self._merge_anomalies(session, anomalies)
            self._merge_incidents(session, incidents)
            self._merge_alerts(session, alerts)
            session.commit()

    # -- Lectures ------------------------------------------------------------

    def list_alerts(self, output_id: str | None = None, limit: int = 100) -> list[Alert]:
        with self._session() as session:
            query = session.query(AlertRow)
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
            return [_incident_from_row(r) for r in rows]

    def list_incidents(self, statuses: list[str] | None = None, limit: int = 50) -> list[Incident]:
        with self._session() as session:
            query = session.query(IncidentRow)
            if statuses:
                query = query.filter(IncidentRow.status.in_(statuses))
            rows = query.order_by(IncidentRow.last_seen_at.desc()).limit(limit).all()
            return [_incident_from_row(r) for r in rows]

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._session() as session:
            row = session.query(IncidentRow).filter(IncidentRow.id == incident_id).one_or_none()
            return _incident_from_row(row) if row is not None else None

    def purge_older_than(self, cutoff: datetime) -> None:
        cutoff_utc = _to_utc(cutoff)
        with self._session() as session:
            session.query(RawMeasurementRow).filter(RawMeasurementRow.timestamp < cutoff_utc).delete()
            session.query(ForecastRow).filter(ForecastRow.timestamp < cutoff_utc).delete()
            session.query(AnomalyRow).filter(AnomalyRow.timestamp < cutoff_utc).delete()
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
        score=row.score,
        peak_traffic_deviation_percent=row.peak_traffic_deviation_percent,
        peak_loss_percent=row.peak_loss_percent,
        description=row.description,
        # `model_dump(mode="json")` sérialise les timestamps en ISO 8601 UTC ;
        # Pydantic les re-parse directement en datetime timezone-aware.
        evidence=[IncidentEvidence.model_validate(e) for e in row.evidence],
    )
