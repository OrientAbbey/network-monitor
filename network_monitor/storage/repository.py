"""
Stockage (§29) : quatre tables séparées (mesures, prévisions, anomalies,
incidents) pour éviter qu'un incident ne duplique les observations brutes.

Sécurité : toutes les requêtes passent par l'ORM SQLAlchemy (paramétrées
nativement, jamais de SQL construit par concaténation de chaînes) ; la
sérialisation des `evidence` utilise JSON via Pydantic, jamais `pickle`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from network_monitor.app.models import (
    AnomalyResult,
    ForecastModelName,
    ForecastResult,
    Incident,
    TrafficSample,
)


class Base(DeclarativeBase):
    pass


def _to_utc(dt: datetime | None) -> datetime | None:
    """Normalise en UTC avant écriture (SQLite ne conserve pas le fuseau horaire)."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Ré-attache le fuseau UTC à la lecture (perdu par le driver SQLite, §3 exige timezone-aware)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class RawMeasurementRow(Base):
    __tablename__ = "raw_measurements"
    output_id = Column(String, primary_key=True)
    timestamp = Column(DateTime(timezone=True), primary_key=True)
    traffic_mbps = Column(Float, nullable=False)
    loss_percent = Column(Float, nullable=False)


class ForecastRow(Base):
    __tablename__ = "forecasts"
    output_id = Column(String, primary_key=True)
    timestamp = Column(DateTime(timezone=True), primary_key=True)
    model_name = Column(String, nullable=False)
    predicted_traffic_mbps = Column(Float, nullable=False)
    lower_bound_mbps = Column(Float, nullable=False)
    upper_bound_mbps = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)


class AnomalyRow(Base):
    __tablename__ = "anomalies"
    output_id = Column(String, primary_key=True)
    timestamp = Column(DateTime(timezone=True), primary_key=True)
    traffic_anomaly_score = Column(Float, nullable=False)
    loss_anomaly_score = Column(Float, nullable=False)
    persistence_score = Column(Float, nullable=False)
    joint_anomaly_score = Column(Float, nullable=False)
    data_gap = Column(String, nullable=False)  # "0"/"1", évite les soucis de portage bool SQLite


class IncidentRow(Base):
    __tablename__ = "incidents"
    id = Column(String, primary_key=True)
    output_id = Column(String, nullable=False)
    status = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    incident_type = Column(String, nullable=False)
    suspected_at = Column(DateTime(timezone=True), nullable=True)
    opened_at = Column(DateTime(timezone=True), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    score = Column(Float, nullable=False)
    peak_traffic_deviation_percent = Column(Float, nullable=False)
    peak_loss_percent = Column(Float, nullable=False)
    description = Column(String, nullable=False)
    # Les preuves sont propres à l'incident (résumés, pas les mesures brutes,
    # §29) : un JSON compact est plus simple qu'une table de jointure pour
    # un volume par incident qui reste faible.
    evidence = Column(JSON, nullable=False)


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
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine, future=True)

    def _session(self) -> Session:
        return self._session_factory()

    def save_samples(self, samples: list[TrafficSample]) -> None:
        with self._session() as session:
            for s in samples:
                session.merge(
                    RawMeasurementRow(
                        output_id=s.output_id,
                        timestamp=_to_utc(s.timestamp),
                        traffic_mbps=s.traffic_mbps,
                        loss_percent=s.loss_percent,
                    )
                )
            session.commit()

    def save_forecasts(self, forecasts: list[ForecastResult]) -> None:
        with self._session() as session:
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
            session.commit()

    def save_anomalies(self, anomalies: list[AnomalyResult]) -> None:
        with self._session() as session:
            for a in anomalies:
                session.merge(
                    AnomalyRow(
                        output_id=a.output_id,
                        timestamp=_to_utc(a.timestamp),
                        traffic_anomaly_score=a.traffic_anomaly_score,
                        loss_anomaly_score=a.loss_anomaly_score,
                        persistence_score=a.persistence_score,
                        joint_anomaly_score=a.joint_anomaly_score,
                        data_gap="1" if a.data_gap else "0",
                    )
                )
            session.commit()

    def upsert_incident(self, incident: Incident) -> None:
        with self._session() as session:
            session.merge(
                IncidentRow(
                    id=incident.id,
                    output_id=incident.output_id,
                    status=incident.status.value,
                    severity=incident.severity.value,
                    incident_type=incident.incident_type.value,
                    suspected_at=_to_utc(incident.suspected_at),
                    opened_at=_to_utc(incident.opened_at),
                    last_seen_at=_to_utc(incident.last_seen_at),
                    closed_at=_to_utc(incident.closed_at),
                    score=incident.score,
                    peak_traffic_deviation_percent=incident.peak_traffic_deviation_percent,
                    peak_loss_percent=incident.peak_loss_percent,
                    description=incident.description,
                    # `mode="json"` produit des types déjà sérialisables (dates en ISO 8601).
                    evidence=[e.model_dump(mode="json") for e in incident.evidence],
                )
            )
            session.commit()

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
                    data_gap=r.data_gap == "1",
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


def _incident_from_row(row: IncidentRow) -> Incident:
    from network_monitor.app.models import IncidentEvidence

    return Incident(
        id=row.id,
        output_id=row.output_id,
        status=row.status,
        severity=row.severity,
        incident_type=row.incident_type,
        suspected_at=_as_utc(row.suspected_at),
        opened_at=_as_utc(row.opened_at),
        last_seen_at=_as_utc(row.last_seen_at),
        closed_at=_as_utc(row.closed_at),
        score=row.score,
        peak_traffic_deviation_percent=row.peak_traffic_deviation_percent,
        peak_loss_percent=row.peak_loss_percent,
        description=row.description,
        # `model_dump(mode="json")` sérialise les timestamps en ISO 8601 UTC ;
        # Pydantic les re-parse directement en datetime timezone-aware.
        evidence=[IncidentEvidence.model_validate(e) for e in row.evidence],
    )
