"""
Sources de données réelles (§6) : relecture d'un export CSV, lecture d'une
table SQL. Chacune n'a qu'à convertir ses lignes en `TrafficSample` pour
respecter le contrat `DataSource` ; le moteur d'analyse ne les distingue pas
du Mock.

Une ligne invalide (horodatage illisible, valeur négative ou hors bornes) est
écartée avec un avertissement au niveau de la source : ces données n'ont
aucune représentation valide dans `TrafficSample`.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import DateTime, column, create_engine, select, table

from network_monitor.app.config import AppConfig
from network_monitor.app.models import TrafficSample
from network_monitor.data.mock import MockDataSource
from network_monitor.data.source import DataSource

logger = logging.getLogger(__name__)

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FIELDS = ("timestamp", "output_id", "traffic_mbps", "loss_percent")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _sample(timestamp: object, output_id: object, traffic: object, loss: object) -> TrafficSample | None:
    try:
        ts = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(str(timestamp))
        return TrafficSample(
            timestamp=_utc(ts), output_id=str(output_id), traffic_mbps=float(traffic), loss_percent=float(loss)  # type: ignore[arg-type]
        )
    except (ValueError, TypeError, ValidationError):
        logger.warning("Ligne ignorée (invalide) : %r", (timestamp, output_id, traffic, loss))
        return None


class CsvDataSource(DataSource):
    """
    Rejoue un export CSV (colonnes : timestamp, output_id, traffic_mbps,
    loss_percent). Avant `start_realtime(t)`, tout est de l'historique ; après,
    chaque `get_latest()` rend le pas suivant — comme le Mock, mais sur de
    vraies mesures.
    """

    def __init__(self, path: str | Path, interval: timedelta = timedelta(minutes=5)) -> None:
        self._interval = interval
        with Path(path).open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            missing = set(_FIELDS) - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"Colonnes manquantes dans {path} : {sorted(missing)}")
            parsed = (_sample(*(row[f] for f in _FIELDS)) for row in reader)
            self._samples = sorted((s for s in parsed if s is not None), key=lambda s: s.timestamp)
        if not self._samples:
            raise ValueError(f"Aucune ligne exploitable dans {path}")
        self._outputs = sorted({s.output_id for s in self._samples})
        self._clock: datetime | None = None

    def default_start(self, fraction: float = 0.8) -> datetime:
        """Instant de bascule historique → rejeu : `fraction` de la durée du fichier."""
        first, last = self._samples[0].timestamp, self._samples[-1].timestamp
        offset = (last - first) * fraction
        return first + timedelta(seconds=round(offset.total_seconds() / self._interval.total_seconds()) * self._interval.total_seconds())

    def list_outputs(self) -> list[str]:
        return list(self._outputs)

    def get_interval(self) -> timedelta:
        return self._interval

    def load_history(self, start: datetime, end: datetime) -> list[TrafficSample]:
        return [s for s in self._samples if start <= s.timestamp < end]

    def start_realtime(self, now: datetime) -> None:
        self._clock = now

    def get_latest(self) -> list[TrafficSample]:
        if self._clock is None:
            raise RuntimeError("Curseur de rejeu non initialisé : appeler start_realtime(now) au préalable.")
        window_start, self._clock = self._clock, self._clock + self._interval
        return [s for s in self._samples if window_start <= s.timestamp < self._clock]


class SqlDataSource(DataSource):
    """
    Lit une table SQL (MariaDB, PostgreSQL, SQLite...) en lecture seule.
    Source « live » : `get_latest()` rend les lignes pas encore vues (à partir
    de l'instant passé à `start_realtime`, inclus — comme le Mock et le CSV,
    dont `load_history` exclut la borne de fin), sans horloge simulée.

    Les noms de table et de colonnes viennent de la configuration : ils sont
    validés comme identifiants simples puis passés par les constructions
    SQLAlchemy (jamais concaténés dans une requête), les valeurs par
    paramètres liés.
    """

    live = True

    def __init__(
        self,
        url: str,
        interval: timedelta = timedelta(minutes=5),
        table_name: str = "traffic_measurements",
        columns: dict[str, str] | None = None,
    ) -> None:
        names = {f: f for f in _FIELDS} | (columns or {})
        for ident in (table_name, *names.values()):
            if not IDENTIFIER.match(ident):
                raise ValueError(f"Identifiant SQL invalide : {ident!r}")
        self._interval = interval
        # pool_pre_ping : MariaDB/MySQL ferment les connexions inactives
        # (wait_timeout) ; sans cela, le premier tick après une pause échouerait.
        self._engine = create_engine(url, future=True, pool_pre_ping=True, pool_recycle=1800)
        self._table = table(table_name, *(column(names[f], DateTime) if f == "timestamp" else column(names[f]) for f in _FIELDS))
        self._cols = [self._table.c[names[f]] for f in _FIELDS]
        self._ts = self._cols[0]
        with self._engine.connect() as conn:
            self._outputs = sorted(str(r[0]) for r in conn.execute(select(self._cols[1]).distinct()))
        self._next: datetime | None = None  # premier instant pas encore rendu (inclus)

    def _query(self, *conditions) -> list[TrafficSample]:
        stmt = select(*self._cols).where(*conditions).order_by(self._ts)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [s for s in (_sample(*r) for r in rows) if s is not None]

    @staticmethod
    def _param(value: datetime) -> datetime:
        return _utc(value).replace(tzinfo=None)  # colonnes horodatées en UTC, sans fuseau

    def list_outputs(self) -> list[str]:
        return list(self._outputs)

    def get_interval(self) -> timedelta:
        return self._interval

    def load_history(self, start: datetime, end: datetime) -> list[TrafficSample]:
        return self._query(self._ts >= self._param(start), self._ts < self._param(end))

    def start_realtime(self, now: datetime) -> None:
        self._next = now

    def get_latest(self) -> list[TrafficSample]:
        if self._next is None:
            raise RuntimeError("Curseur non initialisé : appeler start_realtime(now) au préalable.")
        samples = self._query(self._ts >= self._param(self._next))
        if samples:
            self._next = max(s.timestamp for s in samples) + timedelta(microseconds=1)
        return samples

    def dispose(self) -> None:
        self._engine.dispose()


def build_source(config: AppConfig) -> DataSource:
    """Construit la source choisie par `source.type` (§35)."""
    src = config.source
    interval = config.monitoring.interval
    if src.type == "csv":
        assert src.path is not None  # garanti par SourceConfig
        return CsvDataSource(src.path, interval)
    if src.type == "sql":
        assert src.url is not None
        return SqlDataSource(src.url.get_secret_value(), interval, src.table, src.columns)
    return MockDataSource(interval=interval)
