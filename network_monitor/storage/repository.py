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

from sqlalchemy import JSON, Column, DateTime, Float, Index, String, create_engine
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

    # GET /outputs et GET /incidents filtrent/trient sur ces deux colonnes à
    # chaque appel (toutes les 4 s côté dashboard) : sans index, c'est un
    # SCAN complet de la table sur une table qui grossit en continu
    # (REVUE.md, Perf-7).
    __table_args__ = (
        Index("ix_incidents_status", "status"),
        Index("ix_incidents_last_seen", "last_seen_at"),
    )


def _sample_row(s: TrafficSample) -> RawMeasurementRow:
    return RawMeasurementRow(
        output_id=s.output_id,
        timestamp=_to_utc(s.timestamp),
        traffic_mbps=s.traffic_mbps,
        loss_percent=s.loss_percent,
    )


def _forecast_row(f: ForecastResult) -> ForecastRow:
    return ForecastRow(
        output_id=f.output_id,
        timestamp=_to_utc(f.timestamp),
        model_name=f.model_name.value,
        predicted_traffic_mbps=f.predicted_traffic_mbps,
        lower_bound_mbps=f.lower_bound_mbps,
        upper_bound_mbps=f.upper_bound_mbps,
        confidence=f.confidence,
    )


def _anomaly_row(a: AnomalyResult) -> AnomalyRow:
    return AnomalyRow(
        output_id=a.output_id,
        timestamp=_to_utc(a.timestamp),
        traffic_anomaly_score=a.traffic_anomaly_score,
        loss_anomaly_score=a.loss_anomaly_score,
        persistence_score=a.persistence_score,
        joint_anomaly_score=a.joint_anomaly_score,
        data_gap="1" if a.data_gap else "0",
    )


def _incident_row(incident: Incident) -> IncidentRow:
    return IncidentRow(
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
    ) -> None:
        """Équivalent transactionnel à un appel groupé de save_samples/save_forecasts/save_anomalies/upsert_incident."""
        ...

    def dispose(self) -> None:
        """Libère les ressources de stockage. No-op par défaut (implémentations en mémoire, etc.)."""
        return None

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
        if url.startswith("sqlite"):
            # WAL supprime le verrou d'écriture global (meilleure concurrence
            # lecture/écriture entre le thread de monitoring et les requêtes
            # HTTP) et NORMAL n'fsync qu'aux points de contrôle du WAL plutôt
            # qu'à chaque commit — gain mesuré ~23x sur le coût d'un tick
            # (REVUE.md §4, Perf-1), sans perte de durabilité au-delà d'un
            # crash du système d'exploitation lui-même (compromis acceptable
            # pour des données de supervision, régénérables par le Mock).
            with self._engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine, future=True)

    def _session(self) -> Session:
        return self._session_factory()

    def dispose(self) -> None:
        """
        Libère le pool de connexions de l'engine. Sans cet appel, le fichier
        SQLite reste verrouillé après la dernière session fermée (le driver
        garde des connexions ouvertes dans le pool) — inoffensif sous Linux
        (un fichier ouvert peut être supprimé), mais provoque un
        `PermissionError` au teardown des tests sous Windows (REVUE.md P0-3).
        À appeler explicitement en fin de vie du repository : fixtures de
        tests, et arrêt propre de l'application si un jour nécessaire.
        """
        self._engine.dispose()

    def save_samples(self, samples: list[TrafficSample]) -> None:
        with self._session() as session:
            for s in samples:
                session.merge(_sample_row(s))
            session.commit()

    def save_forecasts(self, forecasts: list[ForecastResult]) -> None:
        with self._session() as session:
            for f in forecasts:
                session.merge(_forecast_row(f))
            session.commit()

    def save_anomalies(self, anomalies: list[AnomalyResult]) -> None:
        with self._session() as session:
            for a in anomalies:
                session.merge(_anomaly_row(a))
            session.commit()

    def upsert_incident(self, incident: Incident) -> None:
        with self._session() as session:
            session.merge(_incident_row(incident))
            session.commit()

    def save_tick(
        self,
        samples: list[TrafficSample],
        forecasts: list[ForecastResult],
        anomalies: list[AnomalyResult],
        incidents: list[Incident],
    ) -> None:
        """
        Persiste tout le résultat d'un tick (mesures, prévisions, anomalies,
        incidents touchés) en une seule transaction, au lieu des 7 à 10
        `commit()` séparés qu'impliquerait un appel individuel par sortie et
        par type de résultat (REVUE.md, Perf-1 — complément à WAL : réduit
        aussi le nombre de points de synchronisation, pas seulement leur
        coût unitaire, et rend le tick atomique, ce qui est de toute façon
        la sémantique attendue). `incidents` peut contenir des doublons
        (plusieurs sorties touchant le même incident n'arrive pas en
        pratique, mais `merge()` gère de toute façon l'idempotence par clé).
        """
        with self._session() as session:
            for s in samples:
                session.merge(_sample_row(s))
            for f in forecasts:
                session.merge(_forecast_row(f))
            for a in anomalies:
                session.merge(_anomaly_row(a))
            for i in incidents:
                session.merge(_incident_row(i))
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
