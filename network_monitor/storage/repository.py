"""
Stockage (§29) : quatre tables séparées (mesures, prévisions, anomalies,
incidents) pour éviter qu'un incident ne duplique les observations brutes.

Sécurité : toutes les requêtes passent par l'ORM SQLAlchemy (paramétrées
nativement, jamais de SQL construit par concaténation de chaînes) ; la
sérialisation des `evidence` utilise JSON via Pydantic, jamais `pickle`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Float, Index, Integer, String, create_engine, func, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, defer, sessionmaker

from network_monitor.app.models import (
    Alert,
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
    return dt.astimezone(UTC)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Ré-attache le fuseau UTC à la lecture (perdu par le driver SQLite, §3 exige timezone-aware)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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
    # Colonnes ajoutées après la première version du schéma : nullables pour
    # rester compatibles avec les lignes déjà présentes (voir _migrate_additive).
    traffic_deviation_percent = Column(Float, nullable=True)
    loss_deviation_points = Column(Float, nullable=True)
    traffic_out_of_interval = Column(String, nullable=True)  # "0"/"1"


class AlertRow(Base):
    """Historique des alertes (§27), lié à l'incident qui les a produites."""

    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    incident_id = Column(String, nullable=False)
    output_id = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    event = Column(String, nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    message = Column(String, nullable=False)

    __table_args__ = (
        Index("ix_alerts_incident", "incident_id", "timestamp"),
        Index("ix_alerts_output_ts", "output_id", "timestamp"),
    )


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
    current_score = Column(Float, nullable=True)  # ajouté après coup, voir AnomalyRow
    peak_traffic_deviation_percent = Column(Float, nullable=False)
    peak_loss_percent = Column(Float, nullable=False)
    description = Column(String, nullable=False)
    # Les preuves sont propres à l'incident (résumés, pas les mesures brutes,
    # §29) : un JSON compact est plus simple qu'une table de jointure pour
    # un volume par incident qui reste faible.
    evidence = Column(JSON, nullable=False)

    # /outputs et /incidents filtrent et trient sur ces colonnes à chaque rafraîchissement du dashboard.
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
        traffic_deviation_percent=a.traffic_deviation_percent,
        loss_deviation_points=a.loss_deviation_points,
        traffic_out_of_interval="1" if a.traffic_out_of_interval else "0",
    )


def _alert_row(a: Alert) -> AlertRow:
    return AlertRow(
        incident_id=a.incident_id,
        output_id=a.output_id,
        severity=a.severity.value,
        event=a.event.value,
        timestamp=_to_utc(a.timestamp),
        message=a.message,
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
        current_score=incident.current_score,
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
        alerts: list[Alert] | None = None,
    ) -> None:
        """Équivalent transactionnel à un appel groupé de save_samples/save_forecasts/save_anomalies/upsert_incident/save_alerts."""
        ...

    @abstractmethod
    def save_alerts(self, alerts: list[Alert]) -> None: ...

    @abstractmethod
    def get_alerts(
        self,
        output_id: str | None = None,
        incident_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[Alert]:
        """Alertes, de la plus récente à la plus ancienne."""
        ...

    @abstractmethod
    def list_incidents_overlapping(self, start: datetime, end: datetime) -> list[Incident]:
        """Incidents (tous statuts) dont la période recoupe [start, end]."""
        ...

    @abstractmethod
    def purge_before(self, cutoff: datetime) -> dict[str, int]:
        """
        Supprime les mesures, prévisions et anomalies antérieures à `cutoff` (les tables qui grossissent
        en continu). Incidents et alertes sont conservés. Renvoie le nombre de lignes supprimées par table.
        """
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
    def list_active_incidents(self, with_evidence: bool = True) -> list[Incident]:
        """
        Incidents SUSPECT, OPEN ou STALE. `with_evidence=False` ne charge pas les preuves (`evidence=[]`) :
        réservé à la lecture. Un incident ainsi chargé ne doit jamais être réécrit par `upsert_incident`.
        """
        ...

    @abstractmethod
    def list_incidents(
        self, statuses: list[str] | None = None, limit: int = 50, with_evidence: bool = True
    ) -> list[Incident]: ...

    @abstractmethod
    def get_incident(self, incident_id: str) -> Incident | None: ...


class SqlAlchemyRepository(Repository):
    """
    Implémentation par défaut, sur SQLite par un fichier local (gratuit,
    sans dépendance externe — adapté à un environnement contraint).
    """

    def __init__(self, url: str = "sqlite:///network_monitor.db") -> None:
        # check_same_thread=False : la boucle de monitoring et les requêtes HTTP tournent dans des
        # threads différents. Sûr tant que chaque appel ouvre sa propre session (seul l'engine est partagé).
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self._engine = create_engine(url, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            # WAL : les lectures HTTP ne bloquent plus l'écriture du thread de monitoring.
            # synchronous=NORMAL : fsync aux points de contrôle du WAL plutôt qu'à chaque commit ;
            # seul un crash du système (pas de l'application) peut perdre les derniers commits.
            with self._engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
                conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        Base.metadata.create_all(self._engine)
        self._migrate_additive()
        self._session_factory = sessionmaker(bind=self._engine, future=True)

    def _session(self) -> Session:
        return self._session_factory()

    def _migrate_additive(self) -> None:
        """
        `create_all` ne modifie pas une table existante : une base créée par une version précédente
        échouerait à la première requête. On ajoute donc les colonnes (nullables) qui manquent.
        Ni renommage ni suppression : au-delà, utiliser Alembic.
        """
        inspector = inspect(self._engine)
        with self._engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                existing = {c["name"] for c in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in existing:
                        continue
                    if not column.nullable:
                        raise RuntimeError(
                            f"Colonne non nullable manquante {table.name}.{column.name} : "
                            "migration automatique impossible, supprimer la base ou utiliser Alembic."
                        )
                    ddl_type = column.type.compile(dialect=self._engine.dialect)
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'))

    def dispose(self) -> None:
        """Ferme le pool de connexions. Sans cela, le fichier SQLite reste verrouillé sous Windows."""
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
        alerts: list[Alert] | None = None,
    ) -> None:
        """Persiste tout le résultat d'un tick en une seule transaction. Mesures, prévisions, anomalies et incidents sont idempotents (merge par clé)."""
        with self._session() as session:
            for s in samples:
                session.merge(_sample_row(s))
            for f in forecasts:
                session.merge(_forecast_row(f))
            for a in anomalies:
                session.merge(_anomaly_row(a))
            for i in incidents:
                session.merge(_incident_row(i))
            for al in alerts or []:
                session.add(_alert_row(al))
            session.commit()

    def save_alerts(self, alerts: list[Alert]) -> None:
        with self._session() as session:
            for al in alerts:
                session.add(_alert_row(al))
            session.commit()

    def get_alerts(
        self,
        output_id: str | None = None,
        incident_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[Alert]:
        with self._session() as session:
            query = session.query(AlertRow)
            if output_id is not None:
                query = query.filter(AlertRow.output_id == output_id)
            if incident_id is not None:
                query = query.filter(AlertRow.incident_id == incident_id)
            if start is not None:
                query = query.filter(AlertRow.timestamp >= _to_utc(start))
            if end is not None:
                query = query.filter(AlertRow.timestamp < _to_utc(end))
            rows = query.order_by(AlertRow.timestamp.desc(), AlertRow.id.desc()).limit(limit).all()
            return [
                Alert(
                    incident_id=r.incident_id,
                    output_id=r.output_id,
                    severity=r.severity,
                    event=r.event,
                    timestamp=_as_utc(r.timestamp),
                    message=r.message,
                )
                for r in rows
            ]

    def list_incidents_overlapping(self, start: datetime, end: datetime) -> list[Incident]:
        first_seen: Any = func.coalesce(IncidentRow.suspected_at, IncidentRow.opened_at, IncidentRow.last_seen_at)
        with self._session() as session:
            rows = (
                session.query(IncidentRow)
                .filter(IncidentRow.last_seen_at >= _to_utc(start), first_seen <= _to_utc(end))
                .order_by(IncidentRow.last_seen_at)
                .all()
            )
            return [_incident_from_row(r) for r in rows]

    def purge_before(self, cutoff: datetime) -> dict[str, int]:
        cutoff = _to_utc(cutoff)
        deleted: dict[str, int] = {}
        with self._session() as session:
            for name, row in (("measurements", RawMeasurementRow), ("forecasts", ForecastRow), ("anomalies", AnomalyRow)):
                deleted[name] = session.query(row).filter(row.timestamp < cutoff).delete(synchronize_session=False)
            session.commit()
        return deleted

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
                    traffic_deviation_percent=r.traffic_deviation_percent,
                    loss_deviation_points=r.loss_deviation_points,
                    traffic_out_of_interval=r.traffic_out_of_interval == "1",
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

    def list_active_incidents(self, with_evidence: bool = True) -> list[Incident]:
        active_statuses = ["suspect", "open", "stale"]
        with self._session() as session:
            query = session.query(IncidentRow).filter(IncidentRow.status.in_(active_statuses))
            if not with_evidence:
                query = query.options(defer(IncidentRow.evidence))
            return [_incident_from_row(r, with_evidence) for r in query.all()]

    def list_incidents(
        self, statuses: list[str] | None = None, limit: int = 50, with_evidence: bool = True
    ) -> list[Incident]:
        with self._session() as session:
            query = session.query(IncidentRow)
            if statuses:
                query = query.filter(IncidentRow.status.in_(statuses))
            if not with_evidence:
                query = query.options(defer(IncidentRow.evidence))
            rows = query.order_by(IncidentRow.last_seen_at.desc()).limit(limit).all()
            return [_incident_from_row(r, with_evidence) for r in rows]

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._session() as session:
            row = session.query(IncidentRow).filter(IncidentRow.id == incident_id).one_or_none()
            return _incident_from_row(row) if row is not None else None


def _incident_from_row(row: IncidentRow, with_evidence: bool = True) -> Incident:
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
        current_score=row.current_score,
        peak_traffic_deviation_percent=row.peak_traffic_deviation_percent,
        peak_loss_percent=row.peak_loss_percent,
        description=row.description,
        # `model_dump(mode="json")` sérialise les timestamps en ISO 8601 UTC ;
        # Pydantic les re-parse directement en datetime timezone-aware.
        evidence=[IncidentEvidence.model_validate(e) for e in row.evidence] if with_evidence else [],
    )
