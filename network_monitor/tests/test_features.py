"""Features, Isolation Forest et écriture groupée d'un tick."""

from collections import deque
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from network_monitor.analysis.detection import IsolationForestDetector
from network_monitor.analysis.features import compute_features, history_steps
from network_monitor.analysis.features_extended import (
    EXTENDED_KEYS,
    compute_extended_features,
)
from network_monitor.app.config import AppConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import Severity, TrafficSample
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.storage.repository import SqlAlchemyRepository

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


def _samples(n: int, traffic=100.0, loss=0.1) -> list[TrafficSample]:
    return [
        TrafficSample(timestamp=NOW + i * STEP, output_id="O", traffic_mbps=traffic + i, loss_percent=loss)
        for i in range(n)
    ]


def test_v1_features():
    recent = _samples(12)
    cur = TrafficSample(timestamp=NOW + 12 * STEP, output_id="O", traffic_mbps=120.0, loss_percent=0.6)
    f = compute_features(cur, expected_traffic_mbps=100.0, expected_loss_percent=0.1, recent=recent)
    assert f["traffic_vs_expected"] == pytest.approx(1.2)
    assert f["loss_vs_expected"] == pytest.approx(0.5)
    assert f["rolling_std_1h"] > 0
    assert f["hour"] == 1 and f["day_of_week"] == NOW.weekday()
    # attendu nul : ratio neutre plutôt qu'une division par zéro
    assert compute_features(cur, 0.0, 0.0, [])["traffic_vs_expected"] == 1.0


def test_extended_features_need_a_full_hour():
    cur = TrafficSample(timestamp=NOW + 12 * STEP, output_id="O", traffic_mbps=112.0, loss_percent=0.1)
    assert history_steps(STEP) == 12
    assert compute_extended_features(cur, _samples(11), STEP) is None
    feats = compute_extended_features(cur, _samples(12), STEP)
    assert feats is not None and set(EXTENDED_KEYS) <= set(feats)
    assert feats["traffic_change_5m"] == pytest.approx(112.0 - 111.0)
    assert feats["traffic_change_1h"] == pytest.approx(112.0 - 100.0)


def _mock_history(days: int, hours_extra: int = 2):
    src = MockDataSource(seed=1)
    all_samples = src.load_history(NOW - timedelta(days=days), NOW + timedelta(hours=hours_extra))
    return [s for s in all_samples if s.output_id == "OUT_001"]


def test_isolation_forest_separates_anomalies_from_normal_behaviour():
    samples = _mock_history(10)
    train = [s for s in samples if s.timestamp < NOW]
    held_out = [s for s in samples if s.timestamp >= NOW]
    detector = IsolationForestDetector("OUT_001", STEP, n_estimators=50)
    detector.fit(train)

    recent: deque[TrafficSample] = deque(train[-12:], maxlen=12)
    normal_scores = []
    for s in held_out:
        score = detector.score(s, recent)
        assert score is not None and 0.0 <= score <= 1.0
        normal_scores.append(score)
        recent.append(s)

    last = held_out[-1]
    weird = last.model_copy(update={"traffic_mbps": last.traffic_mbps * 0.05, "loss_percent": last.loss_percent + 15})
    weird_score = detector.score(weird, deque(held_out[-13:-1], maxlen=12))
    assert weird_score is not None and weird_score > max(normal_scores)


def test_isolation_forest_score_is_none_until_a_full_hour_is_known():
    train = [s for s in _mock_history(10) if s.timestamp < NOW]
    detector = IsolationForestDetector("OUT_001", STEP, n_estimators=20)
    detector.fit(train)
    assert detector.score(train[-1], deque(train[-3:], maxlen=12)) is None


def _run(tmp_path, name: str, forest: bool, ticks: int = 40):
    config = AppConfig.model_validate(
        {
            "history": {"minimum": "7d", "preferred": "10d"},
            "detection": {
                "isolation_forest": {"enabled": forest, "n_estimators": 30},
                "extended_features": {"enabled": True},
            },
        }
    )
    source = MockDataSource(seed=1)
    source.add_scenario(
        AnomalyScenario(
            scenario_id="S", output_id="OUT_001", start_time=NOW + 2 * STEP, end_time=NOW + 30 * STEP,
            type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL, traffic_drop_fraction=0.7,
        )
    )
    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / name}")
    loop = MonitoringLoop(source, repo, config)
    loop.calibrate(NOW)
    loop.run_forever(NOW, max_ticks=ticks)
    return repo


def test_isolation_forest_only_enriches_and_never_changes_decisions(tmp_path):
    def confirmed(repo):
        return sorted((i.output_id, i.opened_at, i.incident_type.value) for i in repo.list_incidents(limit=1000) if i.opened_at)

    off, on = _run(tmp_path, "off.db", False), _run(tmp_path, "on.db", True)
    try:
        assert confirmed(on) == confirmed(off) and confirmed(off)  # mêmes incidents, et il y en a bien un
        anomalies = on.get_anomalies("OUT_001", NOW, NOW + timedelta(hours=4))
        assert anomalies and all(a.isolation_score is not None for a in anomalies)
        assert all("traffic_change_1h" in a.features and "traffic_vs_expected" in a.features for a in anomalies)
        assert all(a.isolation_score is None for a in off.get_anomalies("OUT_001", NOW, NOW + timedelta(hours=4)))
        scores = [a.isolation_score or 0.0 for a in anomalies]
        # Palier soutenu (chute à 30 % de l'attendu, de la 3e à la 30e minute) : la forêt « niveau »
        # le juge inhabituel à chaque pas, y compris longtemps après la transition.
        assert min(scores[5:28]) >= 0.9
        assert sum(scores[:2]) / 2 < 0.2
    finally:
        on.dispose()
        off.dispose()


def test_a_tick_is_written_in_a_single_commit(tmp_path):
    config = AppConfig.model_validate({"history": {"minimum": "7d", "preferred": "10d"}})
    repo = SqlAlchemyRepository(f"sqlite:///{tmp_path / 'tick.db'}")
    try:
        loop = MonitoringLoop(MockDataSource(seed=1), repo, config)
        loop.calibrate(NOW)
        loop._source.start_realtime(NOW)  # type: ignore[attr-defined]
        commits: list[int] = []
        event.listen(repo._engine, "commit", lambda _conn: commits.append(1))
        loop.run_tick(NOW + STEP)
        assert len(commits) == 1
    finally:
        repo.dispose()
