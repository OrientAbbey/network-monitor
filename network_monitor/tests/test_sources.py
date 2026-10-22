"""Contrat commun des sources de données : Mock, CSV, SQL."""

import csv
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)

from network_monitor.app.config import AppConfig
from network_monitor.app.models import TrafficSample
from network_monitor.data.mock import MockDataSource
from network_monitor.data.sources import CsvDataSource, SqlDataSource, build_source

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
SPLIT = T0 + timedelta(days=1)
STEP = timedelta(minutes=5)
ROWS = [
    (T0 + i * STEP, out, 100.0 + i % 7, 0.1)
    for i in range(2 * 288)
    for out in ("OUT_A", "OUT_B")
]


MARIADB_URL = os.environ.get("NM_TEST_MARIADB_URL")  # ex. mysql+pymysql://nm:nmpass@localhost/nmtest
"""MariaDB/MySQL réel : ignoré si la variable n'est pas définie (le CI n'en a pas forcément)."""


def _fill(url: str) -> SqlDataSource:
    engine = create_engine(url)
    meta = MetaData()
    measurements = Table(
        "traffic_measurements", meta,
        Column("timestamp", DateTime), Column("output_id", String(64)),
        Column("traffic_mbps", Float), Column("loss_percent", Float),
    )
    meta.drop_all(engine)
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            measurements.insert(),
            [
                {"timestamp": ts.replace(tzinfo=None), "output_id": o, "traffic_mbps": t, "loss_percent": loss}
                for ts, o, t, loss in ROWS
            ],
        )
    engine.dispose()
    return SqlDataSource(url, STEP)


@pytest.fixture(params=["mock", "csv", "sql", "mariadb"])
def source(request, tmp_path):
    if request.param == "mariadb":
        if not MARIADB_URL:
            pytest.skip("NM_TEST_MARIADB_URL non défini")
        src = _fill(MARIADB_URL)
        yield src
        src.dispose()
        return
    if request.param == "mock":
        yield MockDataSource(seed=1)
    elif request.param == "csv":
        path = tmp_path / "export.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["timestamp", "output_id", "traffic_mbps", "loss_percent"])
            w.writerows((ts.isoformat(), o, t, loss) for ts, o, t, loss in ROWS)
        yield CsvDataSource(path, STEP)
    else:
        url = f"sqlite:///{tmp_path / 'src.db'}"
        engine = create_engine(url)
        meta = MetaData()
        measurements = Table(
            "traffic_measurements", meta,
            Column("timestamp", DateTime), Column("output_id", String),
            Column("traffic_mbps", Float), Column("loss_percent", Float),
        )
        meta.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                measurements.insert(),
                [
                    {"timestamp": ts.replace(tzinfo=None), "output_id": o, "traffic_mbps": t, "loss_percent": loss}
                    for ts, o, t, loss in ROWS
                ],
            )
        engine.dispose()
        src = SqlDataSource(url, STEP)
        yield src
        src.dispose()


def test_contract_outputs_and_interval(source):
    outputs = source.list_outputs()
    assert outputs and all(isinstance(o, str) for o in outputs)
    assert source.get_interval() == STEP


def test_contract_history_bounds_and_order(source):
    start, end = T0 + timedelta(hours=2), T0 + timedelta(hours=6)
    history = source.load_history(start, end)
    assert history and all(isinstance(s, TrafficSample) for s in history)
    assert all(start <= s.timestamp < end for s in history)  # début inclus, fin exclue
    assert [s.timestamp for s in history] == sorted(s.timestamp for s in history)
    assert all(s.timestamp.tzinfo is not None for s in history)


def test_contract_latest_is_new_and_never_repeated(source):
    source.start_realtime(SPLIT)
    seen: set[tuple[str, datetime]] = set()
    for _ in range(3):
        for s in source.get_latest():
            assert s.timestamp >= SPLIT  # `load_history(.., SPLIT)` exclut SPLIT : pas de trou ni de doublon
            key = (s.output_id, s.timestamp)
            assert key not in seen
            seen.add(key)


def test_csv_ignores_invalid_rows(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "timestamp,output_id,traffic_mbps,loss_percent\n"
        "2026-09-01T00:00:00+00:00,A,10,0.1\n"
        "pas-une-date,A,10,0.1\n"
        "2026-09-01T00:05:00+00:00,A,-5,0.1\n",
        encoding="utf-8",
    )
    assert len(CsvDataSource(path).load_history(T0, T0 + timedelta(hours=1))) == 1


def test_sql_rejects_injection_in_identifiers(tmp_path):
    with pytest.raises(ValueError):
        SqlDataSource(f"sqlite:///{tmp_path / 'x.db'}", table_name="t; DROP TABLE u")


def test_sql_url_never_serialized():
    cfg = AppConfig(source={"type": "sql", "url": "mysql://user:hunter2@host/db"})
    assert "hunter2" not in cfg.model_dump_json()
    assert build_source(AppConfig()).__class__ is MockDataSource


def test_source_config_requires_its_parameters():
    with pytest.raises(ValueError):
        AppConfig(source={"type": "csv"})
    with pytest.raises(ValueError):
        AppConfig(source={"type": "sql"})


@pytest.mark.skipif(not MARIADB_URL, reason="NM_TEST_MARIADB_URL non défini")
def test_mariadb_source_survives_a_dropped_connection():
    """Le serveur ferme une connexion du pool (wait_timeout, redémarrage) : le tick suivant doit passer."""
    assert MARIADB_URL is not None
    src = _fill(MARIADB_URL)
    try:
        assert src.load_history(T0, T0 + timedelta(hours=1))  # une connexion est maintenant dans le pool
        killer = create_engine(MARIADB_URL)
        with killer.connect() as conn:
            for (pid, *_rest) in conn.execute(text("SHOW PROCESSLIST")).all():
                if pid != conn.execute(text("SELECT CONNECTION_ID()")).scalar():
                    conn.execute(text(f"KILL {int(pid)}"))
        killer.dispose()
        assert src.load_history(T0, T0 + timedelta(hours=1))
    finally:
        src.dispose()


@pytest.fixture(params=["sqlite", "mariadb"])
def sql_url(request, tmp_path):
    if request.param == "mariadb":
        if not MARIADB_URL:
            pytest.skip("NM_TEST_MARIADB_URL non défini")
        return MARIADB_URL
    return f"sqlite:///{tmp_path / 'e2e.db'}"


def test_sql_source_drives_the_whole_monitoring_loop(sql_url, tmp_path):
    """Bout en bout : historique lu dans la table, calibration, puis mesures « live » détectées."""
    from network_monitor.app.main import MonitoringLoop
    from network_monitor.storage.repository import SqlAlchemyRepository

    now = T0 + timedelta(days=10)
    samples = MockDataSource(seed=1).load_history(T0, now + timedelta(hours=1))
    engine = create_engine(sql_url)
    meta = MetaData()
    table = Table(
        "traffic_measurements", meta,
        Column("timestamp", DateTime), Column("output_id", String(64)),
        Column("traffic_mbps", Float), Column("loss_percent", Float),
    )
    meta.drop_all(engine)
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert(), [
            {"timestamp": s.timestamp.replace(tzinfo=None), "output_id": s.output_id,
             "traffic_mbps": s.traffic_mbps, "loss_percent": s.loss_percent}
            for s in samples
        ])
    engine.dispose()

    config = AppConfig.model_validate({
        "source": {"type": "sql", "url": sql_url},
        "history": {"minimum": "7d", "preferred": "10d"},
    })
    source = build_source(config)
    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'out.db'}")
    try:
        loop = MonitoringLoop(source, repo, config)
        loop.calibrate(now)
        source.start_realtime(now)
        loop.run_tick(now + STEP)
        for output in source.list_outputs():
            assert repo.get_anomalies(output, now, now + timedelta(hours=2))  # les nouvelles lignes ont été analysées
            assert repo.get_history(output, now, now + timedelta(hours=2))
    finally:
        repo.dispose()
        source.dispose()  # type: ignore[attr-defined]


# -- Parquet : source et archive ----------------------------------------------------------


def _samples(days: int = 3, outputs=("OUT_A", "OUT/B")) -> list[TrafficSample]:
    return [
        TrafficSample(timestamp=ts, output_id=o, traffic_mbps=t, loss_percent=loss)
        for ts, o, t, loss in (
            (T0 + i * STEP, o, 100.0 + i % 7 + k, 0.1) for i in range(days * 288) for k, o in enumerate(outputs)
        )
    ]


def test_archive_roundtrip_is_readable_by_the_parquet_source(tmp_path):
    from network_monitor.data.sources import ParquetDataSource
    from network_monitor.storage.archive import ParquetArchive

    samples = _samples()
    archive = ParquetArchive(tmp_path / "archive")
    assert archive.write(samples) == len(samples)
    # un dossier par sortie (identifiant encodé), un fichier par jour
    assert sorted(p.name for p in (tmp_path / "archive").iterdir()) == ["OUT%2FB", "OUT_A"]
    assert len(list((tmp_path / "archive" / "OUT_A").glob("*.parquet"))) == 3

    src = ParquetDataSource(tmp_path / "archive", STEP)
    assert src.list_outputs() == ["OUT/B", "OUT_A"]
    window = src.load_history(T0 + timedelta(hours=2), T0 + timedelta(hours=6), "OUT/B")
    assert len(window) == 48 and {s.output_id for s in window} == {"OUT/B"}
    assert [s.timestamp for s in window] == sorted(s.timestamp for s in window)
    expected = [s for s in samples if s.output_id == "OUT/B" and T0 + timedelta(hours=2) <= s.timestamp < T0 + timedelta(hours=6)]
    assert [(s.timestamp, s.traffic_mbps) for s in window] == [(s.timestamp, s.traffic_mbps) for s in expected]


def test_archive_write_is_idempotent(tmp_path):
    from network_monitor.data.sources import ParquetDataSource
    from network_monitor.storage.archive import ParquetArchive

    archive = ParquetArchive(tmp_path / "a")
    samples = _samples(days=1, outputs=("O",))
    archive.write(samples)
    archive.write(samples)  # même lot rejoué (reprise après une purge interrompue)
    archive.write(samples[:10])
    got = ParquetDataSource(tmp_path / "a", STEP).load_history(T0, T0 + timedelta(days=2), "O")
    assert len(got) == len(samples)


def test_purge_archives_to_parquet_then_deletes(tmp_path):
    from network_monitor.data.sources import ParquetDataSource
    from network_monitor.storage.archive import ParquetArchive
    from network_monitor.storage.repository import SqlAlchemyRepository

    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'r.db'}", ParquetArchive(tmp_path / "arch"))
    try:
        samples = _samples(days=4, outputs=("O",))
        repo.save_samples(samples)
        cutoff = T0 + timedelta(days=2, hours=6)
        repo.purge_older_than(cutoff)
        assert repo.get_history("O", T0, cutoff) == []  # retiré de la base
        kept = repo.get_history("O", cutoff, T0 + timedelta(days=5))
        assert len(kept) == sum(s.timestamp >= cutoff for s in samples)
        archived = ParquetDataSource(tmp_path / "arch", STEP).load_history(T0, T0 + timedelta(days=5), "O")
        assert len(archived) == len(samples) - len(kept)  # rien de perdu, rien en double
        assert max(s.timestamp for s in archived) < cutoff
        repo.purge_older_than(cutoff)  # rejouer la purge ne change rien
        assert len(ParquetDataSource(tmp_path / "arch", STEP).load_history(T0, T0 + timedelta(days=5), "O")) == len(archived)
    finally:
        repo.dispose()


def test_parquet_single_file_source_drives_replay(tmp_path):
    import polars as pl

    from network_monitor.data.sources import ParquetDataSource

    pl.DataFrame(
        {"timestamp": [s.timestamp for s in _samples(1, ("O",))], "output_id": "O",
         "traffic_mbps": [s.traffic_mbps for s in _samples(1, ("O",))], "loss_percent": 0.1}
    ).write_parquet(tmp_path / "x.parquet")
    src = ParquetDataSource(tmp_path / "x.parquet", STEP)
    start = src.default_start()
    src.start_realtime(start)
    first, second = src.get_latest(), src.get_latest()
    assert first and second and first[0].timestamp == start and second[0].timestamp == start + STEP


def test_polars_csv_accepts_naive_and_offset_timestamps(tmp_path):
    path = tmp_path / "mixed.csv"
    path.write_text(
        "timestamp,output_id,traffic_mbps,loss_percent\n"
        "2026-09-01T00:00:00,A,10,0.1\n"      # naïf : lu comme UTC
        "2026-09-01T02:05:00+02:00,A,11,0.1\n"  # = 00:05 UTC
        "2026-09-01T00:10:00Z,A,12,0.1\n"
        "2026-09-01T00:15:00Z,A,nan,0.1\n"   # NaN écarté
        "2026-09-01T00:20:00Z,A,13,150\n",   # perte hors bornes écartée
        encoding="utf-8",
    )
    got = CsvDataSource(path).load_history(T0, T0 + timedelta(hours=1))
    assert [(s.timestamp, s.traffic_mbps) for s in got] == [(T0, 10.0), (T0 + STEP, 11.0), (T0 + 2 * STEP, 12.0)]


# -- Prometheus ----------------------------------------------------------------------------------

PROM_URL = os.environ.get("NM_TEST_PROMETHEUS_URL")  # ex. http://localhost:9090
"""Vrai Prometheus : ignoré si la variable n'est pas définie."""


def _fake_prometheus(series: dict[str, dict[float, float | str]], calls: list | None = None, fail: list | None = None):
    """Transport httpx qui joue `query_range` : une série par requête PromQL, points hors grille omis."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if fail:
            return httpx.Response(fail.pop(0))
        q = request.url.params["query"]
        start, end, step = (float(request.url.params[k]) for k in ("start", "end", "step"))
        if q not in series:
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})
        if q.startswith("multi"):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": [{"values": []}, {"values": []}]}})
        if q == "boom":
            return httpx.Response(200, json={"status": "error", "errorType": "bad_data", "error": "parse error"})
        points, t = [], start
        while t <= end + 1e-6:
            if t in series[q]:
                points.append([t, str(series[q][t])])
            t += step
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}, "values": points}]}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _prom(series, **kw):
    from network_monitor.data.sources import PrometheusDataSource

    outputs = kw.pop("outputs", {"A": ("traffic_a", "loss_a")})
    return PrometheusDataSource("http://prom", outputs, STEP, client=_fake_prometheus(series, kw.pop("calls", None), kw.pop("fail", None)), **kw)


def _grid(n: int, f=lambda i: 100.0 + i) -> dict[float, float]:
    return {(T0 + i * STEP).timestamp(): f(i) for i in range(n)}


def test_prometheus_history_bounds_and_alignment():
    src = _prom({"traffic_a": _grid(100), "loss_a": _grid(100, lambda i: 0.2)})
    # début au milieu d'un pas : arrondi au pas suivant ; fin exclue
    got = src.load_history(T0 + timedelta(minutes=7), T0 + timedelta(minutes=30), "A")
    assert [s.timestamp for s in got] == [T0 + k * STEP for k in (2, 3, 4, 5)]
    assert got[0].traffic_mbps == 102.0 and got[0].loss_percent == 0.2
    assert src.list_outputs() == ["A"] and src.get_interval() == STEP


def test_prometheus_gap_stays_a_gap_and_bad_values_are_skipped():
    traffic = _grid(10)
    del traffic[(T0 + 3 * STEP).timestamp()]  # trou de collecte
    traffic[(T0 + 5 * STEP).timestamp()] = "NaN"
    traffic[(T0 + 6 * STEP).timestamp()] = "+Inf"
    traffic[(T0 + 7 * STEP).timestamp()] = -5.0  # débit négatif : invalide
    got = _prom({"traffic_a": traffic}).load_history(T0, T0 + 10 * STEP, "A")
    assert [s.timestamp for s in got] == [T0 + k * STEP for k in (0, 1, 2, 4, 8, 9)]
    assert all(s.loss_percent == 0.0 for s in got)  # pas de requête de pertes : 0


def test_prometheus_long_range_is_split_into_chunks():
    calls: list = []
    n = 12_000
    src = _prom({"traffic_a": _grid(n)}, calls=calls, outputs={"A": ("traffic_a", None)})
    got = src.load_history(T0, T0 + n * STEP, "A")
    assert len(got) == n and len(calls) == 3  # 5 000 + 5 000 + 2 000 points
    assert all(int(float(c.url.params["end"]) - float(c.url.params["start"])) // 300 < 11_000 for c in calls)


def test_prometheus_rejects_ambiguous_or_failing_queries():
    from network_monitor.data.sources import PrometheusDataSource

    series: dict[str, dict[float, float | str]] = {"multi_a": {}, "boom": {}}
    client = _fake_prometheus(series)
    with pytest.raises(ValueError, match="2 séries"):
        PrometheusDataSource("http://p", {"A": ("multi_a", None)}, STEP, client=client).load_history(T0, T0 + STEP * 3, "A")
    with pytest.raises(RuntimeError, match="parse error"):
        PrometheusDataSource("http://p", {"A": ("boom", None)}, STEP, client=client).load_history(T0, T0 + STEP * 3, "A")


def test_prometheus_latest_never_skips_or_repeats_and_survives_an_outage():
    import httpx

    now = datetime.now(timezone.utc)
    first = datetime.fromtimestamp((now.timestamp() // 300) * 300 - 20 * 300, timezone.utc)
    series = {"traffic_a": {(first + i * STEP).timestamp(): float(i) for i in range(40)}}
    fail = [503]
    src = _prom(series, lag=timedelta(0), outputs={"A": ("traffic_a", None)}, fail=fail)
    src.start_realtime(first)
    with pytest.raises(httpx.HTTPStatusError):
        src.get_latest()  # Prometheus indisponible : le curseur n'avance pas
    got = src.get_latest()  # il revient : tout ce qui manquait arrive d'un coup
    stamps = [s.timestamp for s in got]
    assert stamps[0] == first and stamps == sorted(set(stamps))
    assert max(stamps) <= now and len(stamps) >= 20
    assert src.get_latest() == []  # rien de nouveau : jamais de doublon


def test_prometheus_sends_bearer_token_and_config_never_exposes_it():
    import httpx

    from network_monitor.data.sources import PrometheusDataSource

    seen: list[httpx.Request] = []
    src = PrometheusDataSource(
        "http://p", {"A": ("q", None)}, STEP, bearer_token="s3cr3t",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={"status": "success", "data": {"result": []}})), headers={"Authorization": "Bearer s3cr3t"}),
    )
    src.load_history(T0, T0 + STEP * 3, "A")
    assert seen[0].headers["authorization"] == "Bearer s3cr3t"
    cfg = AppConfig.model_validate({"source": {"type": "prometheus", "url": "http://user:pw@prom:9090", "prometheus": {"A": {"traffic": "x"}}}})
    assert "pw@" not in cfg.model_dump_json()
    with pytest.raises(ValueError):
        AppConfig.model_validate({"source": {"type": "prometheus", "url": "http://p"}})  # aucune sortie


@pytest.mark.skipif(not PROM_URL, reason="NM_TEST_PROMETHEUS_URL non défini")
def test_real_prometheus_history_and_live_cursor():
    import time

    from network_monitor.data.sources import PrometheusDataSource

    step = timedelta(seconds=5)
    outputs = {"eth0": (
        'rate(node_network_receive_bytes_total{device="eth0"}[30s]) * 8 / 1e6',
        '100 * rate(node_network_receive_drop_total{device="eth0"}[30s]) / rate(node_network_receive_packets_total{device="eth0"}[30s])',
    )}
    assert PROM_URL is not None
    src = PrometheusDataSource(PROM_URL, outputs, step, lag=timedelta(seconds=6))
    try:
        now = datetime.now(timezone.utc)
        history = src.load_history(now - timedelta(minutes=2), now - timedelta(seconds=10), "eth0")
        assert len(history) >= 8
        assert all(s.timestamp.timestamp() % 5 == 0 for s in history)  # sur la grille
        assert all(s.traffic_mbps >= 0 and 0 <= s.loss_percent <= 100 for s in history)
        src.start_realtime(now - timedelta(seconds=30))
        first = src.get_latest()
        time.sleep(11)
        second = src.get_latest()
        assert first and second
        assert max(s.timestamp for s in first) < min(s.timestamp for s in second)  # ni trou ni doublon
    finally:
        src.dispose()
