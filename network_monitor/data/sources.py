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

import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
import polars as pl
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


class _FrameSource(DataSource):
    """
    Base des sources fichiers, adossée à Polars : les données restent en colonnes (quelques
    octets par ligne) au lieu d'objets Python (quelques centaines d'octets par ligne), et on
    ne construit des `TrafficSample` que pour la fenêtre demandée.

    Rejeu : avant `start_realtime(t)`, tout est de l'historique ; après, chaque `get_latest()`
    rend le pas suivant, comme le Mock mais sur de vraies mesures.
    """

    def __init__(self, frame: pl.LazyFrame, interval: timedelta, outputs: list[str]) -> None:
        self._lf = frame
        self._interval = interval
        self._outputs = sorted(outputs)
        self._clock: datetime | None = None
        bounds = frame.select(pl.col("timestamp").min().alias("lo"), pl.col("timestamp").max().alias("hi")).collect()
        self._first, self._last = bounds["lo"][0], bounds["hi"][0]
        if self._first is None:
            raise ValueError("Aucune ligne exploitable dans la source")

    def default_start(self, fraction: float = 0.8) -> datetime:
        """Instant de bascule historique → rejeu : `fraction` de la durée couverte."""
        step = self._interval.total_seconds()
        offset = round((self._last - self._first).total_seconds() * fraction / step) * step
        return self._first + timedelta(seconds=offset)

    def list_outputs(self) -> list[str]:
        return list(self._outputs)

    def get_interval(self) -> timedelta:
        return self._interval

    def _window(self, start: datetime, end: datetime, output_id: str | None = None) -> list[TrafficSample]:
        lf = self._lf.filter((pl.col("timestamp") >= start) & (pl.col("timestamp") < end))
        if output_id is not None:
            lf = lf.filter(pl.col("output_id") == output_id)
        df = lf.sort("timestamp", "output_id").collect()
        return [TrafficSample(**row) for row in df.iter_rows(named=True)]

    def load_history(self, start: datetime, end: datetime, output_id: str | None = None) -> list[TrafficSample]:
        return self._window(start, end, output_id)

    def start_realtime(self, now: datetime) -> None:
        self._clock = now

    def get_latest(self) -> list[TrafficSample]:
        if self._clock is None:
            raise RuntimeError("Curseur de rejeu non initialisé : appeler start_realtime(now) au préalable.")
        window_start, self._clock = self._clock, self._clock + self._interval
        return self._window(window_start, self._clock)


def _normalized(lf: pl.LazyFrame, source: str) -> pl.LazyFrame:
    """Colonnes canoniques, horodatage UTC, lignes invalides écartées (avec un avertissement)."""
    schema = lf.collect_schema()
    missing = set(_FIELDS) - set(schema.names())
    if missing:
        raise ValueError(f"Colonnes manquantes dans {source} : {sorted(missing)}")
    ts = pl.col("timestamp")
    if schema["timestamp"] == pl.Utf8:
        # Un export mêle souvent des horodatages avec décalage (`Z`, `+02:00`) et sans (lu comme
        # UTC) : Polars n'infère qu'un format à partir de la première ligne, donc on les essaie tous.
        ts = pl.coalesce(
            ts.str.to_datetime(format=fmt, time_zone="UTC", strict=False)
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%.f%#z", "%Y-%m-%d %H:%M:%S%.f%#z",
                "%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f",
            )
        )
    elif getattr(schema["timestamp"], "time_zone", None) is None:
        ts = ts.dt.replace_time_zone("UTC")
    else:
        ts = ts.dt.convert_time_zone("UTC")
    lf = lf.select(
        ts.dt.cast_time_unit("us").alias("timestamp"),
        pl.col("output_id").cast(pl.Utf8),
        pl.col("traffic_mbps").cast(pl.Float64, strict=False),
        pl.col("loss_percent").cast(pl.Float64, strict=False),
    )
    valid = (
        pl.col("timestamp").is_not_null()
        & (pl.col("output_id").str.len_chars() > 0)
        # Polars classe NaN au-dessus de tout nombre : `NaN >= 0` serait vrai, d'où `is_finite`.
        & pl.col("traffic_mbps").is_finite()
        & (pl.col("traffic_mbps") >= 0)
        & pl.col("loss_percent").is_finite()
        & pl.col("loss_percent").is_between(0, 100)
    )
    total = lf.select(pl.len()).collect().item()
    lf = lf.filter(valid)
    kept = lf.select(pl.len()).collect().item()
    if kept < total:
        logger.warning("%s : %d ligne(s) invalide(s) ignorée(s) sur %d", source, total - kept, total)
    return lf


class CsvDataSource(_FrameSource):
    """Rejeu d'un export CSV (colonnes : timestamp, output_id, traffic_mbps, loss_percent),
    chargé en colonnes par Polars."""

    def __init__(self, path: str | Path, interval: timedelta = timedelta(minutes=5)) -> None:
        # Tout en texte d'abord : une valeur illisible devient une ligne écartée, pas une erreur.
        raw = pl.read_csv(path, infer_schema=False).lazy()
        lf = _normalized(raw, str(path)).collect().lazy()  # figé en mémoire, en colonnes
        outputs = lf.select(pl.col("output_id").unique()).collect()["output_id"].to_list()
        if not outputs:
            raise ValueError(f"Aucune ligne exploitable dans {path}")
        super().__init__(lf, interval, outputs)


class ParquetDataSource(_FrameSource):
    """
    Lit des mesures au format Parquet, sans les charger : le filtre sur le temps et sur la
    sortie est poussé jusqu'au fichier (statistiques de groupes de lignes).

    `path` peut être un fichier, ou l'archive écrite par `ParquetArchive` (un dossier par
    sortie, un fichier par jour) : seuls les fichiers de la sortie demandée sont ouverts.
    """

    def __init__(self, path: str | Path, interval: timedelta = timedelta(minutes=5)) -> None:
        root = Path(path)
        if root.is_dir():
            self._by_output = {unquote(d.name): sorted(d.glob("*.parquet")) for d in root.iterdir() if d.is_dir()}
            self._by_output = {k: v for k, v in self._by_output.items() if v}
            files = [f for fs in self._by_output.values() for f in fs]
            if not files:
                raise ValueError(f"Aucun fichier Parquet dans {root}")
            outputs = list(self._by_output)
        else:
            self._by_output = {}
            files = [root]
            outputs = []
        lf = _normalized(pl.scan_parquet([str(f) for f in files]), str(path))
        if not outputs:
            outputs = lf.select(pl.col("output_id").unique()).collect()["output_id"].to_list()
        super().__init__(lf, interval, outputs)

    def _window(self, start: datetime, end: datetime, output_id: str | None = None) -> list[TrafficSample]:
        if output_id is not None and self._by_output.get(output_id):
            lf = _normalized(pl.scan_parquet([str(f) for f in self._by_output[output_id]]), output_id)
            lf = lf.filter((pl.col("timestamp") >= start) & (pl.col("timestamp") < end))
            df = lf.filter(pl.col("output_id") == output_id).sort("timestamp").collect()
            return [TrafficSample(**row) for row in df.iter_rows(named=True)]
        return super()._window(start, end, output_id)


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

    def load_history(self, start: datetime, end: datetime, output_id: str | None = None) -> list[TrafficSample]:
        conditions = [self._ts >= self._param(start), self._ts < self._param(end)]
        if output_id is not None:
            conditions.append(self._cols[1] == output_id)
        return self._query(*conditions)

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


class PrometheusDataSource(DataSource):
    """
    Lit des séries Prometheus (`/api/v1/query_range`) : tout ce qui est déjà collecté (SNMP via
    snmp_exporter, node_exporter, gNMI...) devient une source sans rien ré-instrumenter. Les
    requêtes PromQL de chaque sortie (trafic en Mbps, perte en %) sont fournies par la
    configuration : c'est elle qui sait quel compteur 64 bits, quel `rate()` et quelles étiquettes.

    Les instants sont ceux d'une grille alignée sur l'époque (multiples de `interval`), commune
    à l'historique et au temps réel. Prometheus n'évalue que sur cette grille et omet les points
    sans donnée : un trou de collecte reste un trou (§31), jamais une valeur inventée.

    Source « live » : `get_latest()` interroge jusqu'à `maintenant - lag` et n'avance son curseur
    qu'après une réponse réussie, donc une panne de Prometheus ne perd rien : le tick suivant
    rattrape la période manquée.
    """

    live = True
    _MAX_POINTS = 5_000  # l'API refuse plus de 11 000 points par requête ; marge large

    def __init__(
        self,
        url: str,
        outputs: dict[str, tuple[str, str | None]],
        interval: timedelta = timedelta(minutes=5),
        lag: timedelta = timedelta(seconds=30),
        bearer_token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not outputs:
            raise ValueError("Au moins une sortie est nécessaire")
        self._outputs = dict(outputs)
        self._interval = interval
        self._lag = lag
        self._base = url.rstrip("/")
        headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        self._client = client or httpx.Client(timeout=30.0, headers=headers)
        self._next: datetime | None = None

    # -- grille et requêtes ----------------------------------------------------------

    def _step(self) -> float:
        return self._interval.total_seconds()

    def _ceil(self, instant: datetime) -> float:
        step = self._step()
        return math.ceil(instant.timestamp() / step) * step

    def _series(self, promql: str, first: float, last: float) -> dict[float, float]:
        """Valeurs par instant (secondes époque) de `first` à `last` inclus, par tranches."""
        step = self._step()
        values: dict[float, float] = {}
        chunk = self._MAX_POINTS * step
        cursor = first
        while cursor <= last:
            stop = min(last, cursor + chunk - step)
            res = self._client.get(
                f"{self._base}/api/v1/query_range",
                params={"query": promql, "start": f"{cursor:.3f}", "end": f"{stop:.3f}", "step": f"{step:g}"},
            )
            res.raise_for_status()
            body = res.json()
            if body.get("status") != "success":
                raise RuntimeError(f"Prometheus : {body.get('errorType')} {body.get('error')}")
            result = body["data"]["result"]
            if len(result) > 1:
                raise ValueError(f"La requête renvoie {len(result)} séries (une seule attendue) : {promql}")
            for ts, raw in (result[0]["values"] if result else []):
                try:
                    value = float(raw)
                except ValueError:
                    continue
                if math.isfinite(value):  # « NaN » / « +Inf » : pas de mesure
                    values[float(ts)] = value
            cursor = stop + step
        return values

    def _range(self, output_id: str, start: datetime, end: datetime) -> list[TrafficSample]:
        """Mesures de `output_id` aux instants de la grille dans [start, end[."""
        first, last = self._ceil(start), self._ceil(end) - self._step()
        if last < first:
            return []
        traffic_q, loss_q = self._outputs[output_id]
        traffic = self._series(traffic_q, first, last)
        loss = self._series(loss_q, first, last) if loss_q else {}
        samples = []
        for ts in sorted(traffic):
            sample = _sample(datetime.fromtimestamp(ts, timezone.utc), output_id, traffic[ts], loss.get(ts, 0.0))
            if sample is not None:
                samples.append(sample)
        return samples

    # -- contrat DataSource -------------------------------------------------------------

    def list_outputs(self) -> list[str]:
        return sorted(self._outputs)

    def get_interval(self) -> timedelta:
        return self._interval

    def load_history(self, start: datetime, end: datetime, output_id: str | None = None) -> list[TrafficSample]:
        targets = [output_id] if output_id is not None else self.list_outputs()
        merged = [s for o in targets for s in self._range(o, start, end)]
        return sorted(merged, key=lambda s: (s.timestamp, s.output_id))

    def start_realtime(self, now: datetime) -> None:
        self._next = datetime.fromtimestamp(self._ceil(now), timezone.utc)

    def get_latest(self) -> list[TrafficSample]:
        if self._next is None:
            raise RuntimeError("Curseur non initialisé : appeler start_realtime(now) au préalable.")
        horizon = datetime.now(timezone.utc) - self._lag
        end = datetime.fromtimestamp(math.floor(horizon.timestamp() / self._step()) * self._step(), timezone.utc)
        if end < self._next:
            return []
        samples = self.load_history(self._next, end + self._interval)
        self._next = end + self._interval  # seulement après une réponse réussie
        return samples

    def dispose(self) -> None:
        self._client.close()


def build_source(config: AppConfig) -> DataSource:
    """Construit la source choisie par `source.type` (§35)."""
    src = config.source
    interval = config.monitoring.interval
    if src.type == "csv":
        assert src.path is not None  # garanti par SourceConfig
        return CsvDataSource(src.path, interval)
    if src.type == "parquet":
        assert src.path is not None
        return ParquetDataSource(src.path, interval)
    if src.type == "sql":
        assert src.url is not None
        return SqlDataSource(src.url.get_secret_value(), interval, src.table, src.columns)
    if src.type == "prometheus":
        assert src.url is not None
        token = os.environ.get(src.bearer_token_env) if src.bearer_token_env else None
        return PrometheusDataSource(
            src.url.get_secret_value(),
            {name: (o.traffic, o.loss) for name, o in src.prometheus.items()},
            interval, src.lag, token,
        )
    return MockDataSource(interval=interval)
