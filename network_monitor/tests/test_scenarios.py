"""
Tests de scénarios de bout en bout (§34) : Mock -> Forecast -> Detection ->
Incident, pour chaque type minimal requis. Chaque test vérifie le
comportement observable (un incident s'est ouvert ou non) sans jamais
transmettre la vérité terrain du Mock au détecteur.
"""

import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from network_monitor.app.config import AppConfig
from network_monitor.app.main import MonitoringLoop
from network_monitor.app.models import Severity
from network_monitor.data.mock import AnomalyScenario, MockDataSource, ScenarioType
from network_monitor.storage.repository import IncidentRow, SqlAlchemyRepository

NOW = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
OUTPUT = "OUT_001"


def _run(scenario: AnomalyScenario | None, ticks: int) -> list[IncidentRow]:
    """Calibre sur 40 jours (sous le seuil de 45j : baseline saisonnière, rapide et stable ;
    suffisamment au-dessus du minimum de 30j pour un taux de faux positifs réaliste, cf. §11),
    injecte éventuellement un scénario, exécute `ticks` pas et renvoie les lignes
    d'incident enregistrées pour la sortie testée."""
    # TemporaryDirectory (et non tempfile.mktemp, déprécié : course sur le nom
    # de fichier, REVUE.md S-10). `dispose()` libère le fichier SQLite avant
    # le nettoyage du répertoire — indispensable sous Windows (P0-3).
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = SqlAlchemyRepository(f"sqlite:///{tmpdir}/scenario.db")
        try:
            config = AppConfig(history={"minimum": "30d", "preferred": "40d"})

            src = MockDataSource(seed=3)
            if scenario is not None:
                src.add_scenario(scenario)

            loop = MonitoringLoop(src, storage, config)
            loop.calibrate(NOW)
            loop.run_forever(NOW, max_ticks=ticks)

            with storage._session() as session:
                rows = session.query(IncidentRow).filter(IncidentRow.output_id == OUTPUT).all()
                session.expunge_all()
        finally:
            storage.dispose()

    return rows


def _scenario(type_: ScenarioType, duration_minutes: int, **kwargs) -> AnomalyScenario:
    return AnomalyScenario(
        scenario_id="S1",
        output_id=OUTPUT,
        start_time=NOW + timedelta(minutes=5),
        end_time=NOW + timedelta(minutes=5 + duration_minutes),
        type=type_,
        severity=Severity.CRITICAL,
        traffic_drop_fraction=kwargs.pop("traffic_drop_fraction", 0.7),
        loss_offset_points=kwargs.pop("loss_offset_points", 10.0),
        **kwargs,
    )


def test_normal_behavior_never_opens_incident():
    rows = _run(None, ticks=24)  # 2h de fonctionnement normal
    assert all(r.status != "open" for r in rows)


def test_traffic_drop_opens_incident():
    rows = _run(_scenario(ScenarioType.TRAFFIC_DROP, duration_minutes=40), ticks=20)
    assert any(r.opened_at is not None for r in rows)


def test_traffic_spike_opens_incident():
    rows = _run(_scenario(ScenarioType.TRAFFIC_SPIKE, duration_minutes=40, traffic_spike_multiplier=3.0), ticks=20)
    assert any(r.opened_at is not None for r in rows)


def test_high_loss_opens_incident():
    rows = _run(_scenario(ScenarioType.HIGH_LOSS, duration_minutes=40, loss_offset_points=15.0), ticks=20)
    assert any(r.opened_at is not None for r in rows)


def test_traffic_drop_with_high_loss_opens_incident():
    rows = _run(_scenario(ScenarioType.TRAFFIC_DROP_HIGH_LOSS, duration_minutes=40), ticks=20)
    opened = [r for r in rows if r.opened_at is not None]
    assert opened
    assert opened[-1].incident_type == "traffic_drop_with_high_loss"


def test_progressive_degradation_eventually_opens_incident():
    rows = _run(_scenario(ScenarioType.PROGRESSIVE_DEGRADATION, duration_minutes=60), ticks=20)
    assert any(r.opened_at is not None for r in rows)


def test_intermittent_incident_opens_incident():
    rows = _run(
        _scenario(
            ScenarioType.INTERMITTENT_INCIDENT,
            duration_minutes=60,
            intermittent_period=timedelta(minutes=10),
            intermittent_duty_cycle=0.6,
        ),
        ticks=20,
    )
    assert any(r.opened_at is not None for r in rows)


def test_short_incident_is_detected():
    """Un incident court (juste au-dessus de minimum_duration) doit tout de même s'ouvrir."""
    rows = _run(_scenario(ScenarioType.SHORT_INCIDENT, duration_minutes=15), ticks=15)
    assert any(r.opened_at is not None for r in rows)


def test_long_incident_stays_open_across_many_ticks():
    rows = _run(_scenario(ScenarioType.LONG_INCIDENT, duration_minutes=90), ticks=25)
    opened = [r for r in rows if r.opened_at is not None]
    assert opened
    # Un incident long doit rester ouvert plusieurs pas avant toute fermeture.
    assert opened[-1].last_seen_at > opened[-1].opened_at + timedelta(minutes=10)


@pytest.mark.parametrize(
    "scenario_type",
    [ScenarioType.PROGRESSIVE_RECOVERY, ScenarioType.COMBINED],
)
def test_remaining_scenario_types_run_without_error(scenario_type):
    """Les deux derniers types du §7 (non listés explicitement au §34) doivent au moins
    s'exécuter sans erreur ; leur détection n'est pas garantie à ce niveau de sévérité."""
    rows = _run(_scenario(scenario_type, duration_minutes=40), ticks=20)
    assert isinstance(rows, list)  # ne doit pas lever d'exception
