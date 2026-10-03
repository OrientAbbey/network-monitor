"""Calibration sortie par sortie et reprise des sorties non calibrées."""

from datetime import datetime, timedelta, timezone

import pytest

from network_monitor.app.config import AppConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.data.mock import MockDataSource
from network_monitor.storage.repository import SqlAlchemyRepository

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    r = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'c.db'}")
    yield r
    r.dispose()


def _cfg(**history):
    return AppConfig.model_validate({"history": {"minimum": "7d", "preferred": "10d"} | history})


def test_history_is_loaded_one_output_at_a_time(repo):
    source = MockDataSource(seed=1)
    calls: list[str | None] = []
    original = source.load_history
    source.load_history = lambda start, end, output_id=None: calls.append(output_id) or original(start, end, output_id)  # type: ignore[method-assign]
    loop = MonitoringLoop(source, repo, _cfg())
    loop.calibrate(NOW)
    assert calls == ["OUT_001", "OUT_002", "OUT_003"]  # jamais None : tout l'historique n'est jamais en mémoire d'un coup
    assert set(loop._forecast_models) == {"OUT_001", "OUT_002", "OUT_003"}


def test_mock_generates_only_the_requested_output():
    source = MockDataSource(seed=1)
    only = source.load_history(NOW, NOW + timedelta(days=1), "OUT_002")
    assert {s.output_id for s in only} == {"OUT_002"} and len(only) == 288
    everything = source.load_history(NOW, NOW + timedelta(days=1))
    assert {s.output_id for s in everything} == {"OUT_001", "OUT_002", "OUT_003"}
    assert [s.timestamp for s in everything if s.output_id == "OUT_002"] == [s.timestamp for s in only]
    # (les valeurs, elles, diffèrent d'un appel à l'autre : le bruit du Mock vient d'un générateur à état.)


def test_an_output_without_enough_history_is_retried_every_hour_not_every_day(repo):
    cfg = _cfg(minimum="30d", preferred="5d")  # impossible : on ne charge que 5 j alors qu'il en faut 30
    loop = MonitoringLoop(MockDataSource(seed=1), repo, cfg)
    loop.calibrate(NOW)
    assert loop._forecast_models == {}  # aucune sortie calibrée
    runs = []
    loop._recompute_models = lambda now, exclude_ranges: runs.append(now)  # type: ignore[method-assign]
    loop.maybe_recalibrate(NOW + timedelta(minutes=30))
    assert runs == []  # trop tôt
    loop.maybe_recalibrate(NOW + timedelta(minutes=61))
    assert len(runs) == 1  # une heure : on réessaie (sans cela, il faudrait attendre 24 h)


def test_calibrated_outputs_keep_the_daily_recalibration_rhythm(repo):
    loop = MonitoringLoop(MockDataSource(seed=1), repo, _cfg())
    loop.calibrate(NOW)
    assert len(loop._forecast_models) == 3
    runs = []
    loop._recompute_models = lambda now, exclude_ranges: runs.append(now)  # type: ignore[method-assign]
    loop.maybe_recalibrate(NOW + timedelta(hours=2))
    assert runs == []
    loop.maybe_recalibrate(NOW + timedelta(hours=25))
    assert len(runs) == 1


def test_a_failing_output_does_not_stop_the_others(repo, monkeypatch):
    loop = MonitoringLoop(MockDataSource(seed=1), repo, _cfg())
    real = loop._calibrate_output

    def flaky(output_id, history, initial):
        if output_id == "OUT_002":
            raise RuntimeError("ajustement impossible")
        real(output_id, history, initial)

    monkeypatch.setattr(loop, "_calibrate_output", flaky)
    loop.calibrate(NOW)
    assert set(loop._forecast_models) == {"OUT_001", "OUT_003"}
