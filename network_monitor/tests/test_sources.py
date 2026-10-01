"""Contrat commun des sources de données (§6) : Mock, CSV, SQL."""

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
