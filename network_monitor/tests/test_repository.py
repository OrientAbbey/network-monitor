"""Tests du stockage : round-trip et séparation des tables (§29, §34)."""

from datetime import UTC, datetime, timedelta

import pytest

from network_monitor.app.models import (
    AnomalyResult,
    ForecastModelName,
    ForecastResult,
    Incident,
    IncidentEvidence,
    IncidentStatus,
    IncidentType,
    Severity,
    TrafficSample,
)
from network_monitor.storage.repository import SqlAlchemyRepository

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path):
    # dispose() libère le fichier SQLite avant que pytest ne supprime tmp_path (verrou sous Windows).
    path = tmp_path / "test.db"
    r = SqlAlchemyRepository(f"sqlite:///{path}")
    yield r
    r.dispose()


def test_samples_round_trip_preserves_timezone(repo):
    samples = [
        TrafficSample(timestamp=T0 + timedelta(minutes=i * 5), output_id="OUT_001", traffic_mbps=100 + i, loss_percent=0.1)
        for i in range(3)
    ]
    repo.save_samples(samples)
    history = repo.get_history("OUT_001", T0, T0 + timedelta(hours=1))
    assert len(history) == 3
    assert history[0].traffic_mbps == 100.0
    assert history[0].timestamp.tzinfo is not None


def test_save_samples_is_idempotent_on_same_key(repo):
    """Réinsérer la même (output_id, timestamp) ne doit pas dupliquer la ligne (clé primaire composite)."""
    sample = TrafficSample(timestamp=T0, output_id="OUT_001", traffic_mbps=100.0, loss_percent=0.1)
    repo.save_samples([sample])
    repo.save_samples([sample])
    history = repo.get_history("OUT_001", T0, T0 + timedelta(minutes=1))
    assert len(history) == 1


def test_incident_round_trip_with_evidence(repo):
    incident = Incident(
        id="INC1",
        output_id="OUT_017",
        status=IncidentStatus.OPEN,
        severity=Severity.CRITICAL,
        incident_type=IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS,
        opened_at=T0,
        last_seen_at=T0,
        score=0.9,
        evidence=[
            IncidentEvidence(timestamp=T0, traffic_mbps=8.1, expected_traffic_mbps=18.4, traffic_anomaly_score=0.94, loss_anomaly_score=0.91)
        ],
    )
    repo.upsert_incident(incident)

    active = repo.list_active_incidents()
    assert len(active) == 1
    assert active[0].id == "INC1"
    assert active[0].evidence[0].traffic_mbps == 8.1
    assert active[0].opened_at.tzinfo is not None


def test_closed_incident_is_not_listed_as_active(repo):
    incident = Incident(
        id="INC2",
        output_id="OUT_017",
        status=IncidentStatus.OPEN,
        severity=Severity.WARNING,
        incident_type=IncidentType.TRAFFIC_DROP,
        opened_at=T0,
        last_seen_at=T0,
        score=0.8,
    )
    repo.upsert_incident(incident)
    assert len(repo.list_active_incidents()) == 1

    incident.status = IncidentStatus.CLOSED
    incident.closed_at = T0 + timedelta(minutes=20)
    repo.upsert_incident(incident)
    assert repo.list_active_incidents() == []


def test_forecasts_and_anomalies_do_not_affect_raw_measurements_table(repo):
    """§29 : les tables doivent rester indépendantes les unes des autres."""
    repo.save_forecasts(
        [
            ForecastResult(
                output_id="OUT_001", timestamp=T0, predicted_traffic_mbps=100.0,
                lower_bound_mbps=90.0, upper_bound_mbps=110.0, confidence=0.8,
                model_name=ForecastModelName.SEASONAL_BASELINE,
            )
        ]
    )
    repo.save_anomalies(
        [AnomalyResult(output_id="OUT_001", timestamp=T0, traffic_anomaly_score=0.5, loss_anomaly_score=0.2, joint_anomaly_score=0.5)]
    )
    assert repo.get_history("OUT_001", T0, T0 + timedelta(hours=1)) == []


def test_incident_lists_can_skip_evidence_without_altering_what_is_stored(repo):
    incident = Incident(
        id="i1", output_id="OUT_001", status=IncidentStatus.OPEN, severity=Severity.WARNING,
        incident_type=IncidentType.TRAFFIC_DROP, suspected_at=T0, opened_at=T0, last_seen_at=T0 + timedelta(minutes=5),
        score=0.8, description="d",
        evidence=[IncidentEvidence(timestamp=T0, traffic_anomaly_score=0.8, loss_anomaly_score=0.1)],
    )
    repo.upsert_incident(incident)

    assert repo.list_incidents(with_evidence=False)[0].evidence == []
    assert repo.list_active_incidents(with_evidence=False)[0].evidence == []
    assert len(repo.list_incidents()[0].evidence) == 1
    assert len(repo.list_active_incidents()[0].evidence) == 1
    assert len(repo.get_incident("i1").evidence) == 1
