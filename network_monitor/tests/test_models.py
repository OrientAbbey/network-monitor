"""Vérifications rapides des contraintes définies sur les modèles (§3, §13, §16, §19-24)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from network_monitor.app.models import (
    AnomalyResult,
    DataQualityFlag,
    ForecastModelName,
    ForecastResult,
    Incident,
    IncidentEvidence,
    IncidentStatus,
    IncidentType,
    Severity,
    TrafficSample,
    ValidatedSample,
)

NOW = datetime(2026, 8, 1, 18, 0, 0, tzinfo=UTC)


# --- TrafficSample -----------------------------------------------------


def test_traffic_sample_nominal():
    s = TrafficSample(timestamp=NOW, output_id="OUT_001", traffic_mbps=824.5, loss_percent=0.12)
    assert s.traffic_mbps == 824.5


def test_traffic_sample_rejects_naive_timestamp():
    with pytest.raises(ValidationError):
        TrafficSample(
            timestamp=datetime(2026, 8, 1, 18, 0, 0),  # naive, pas de tzinfo
            output_id="OUT_001",
            traffic_mbps=100.0,
            loss_percent=0.1,
        )


def test_traffic_sample_rejects_negative_traffic():
    with pytest.raises(ValidationError):
        TrafficSample(timestamp=NOW, output_id="OUT_001", traffic_mbps=-1.0, loss_percent=0.1)


def test_traffic_sample_rejects_loss_over_100():
    with pytest.raises(ValidationError):
        TrafficSample(timestamp=NOW, output_id="OUT_001", traffic_mbps=100.0, loss_percent=150.0)


# --- ValidatedSample -----------------------------------------------------


def test_validated_sample_missing_is_not_usable():
    vs = ValidatedSample(
        output_id="OUT_001", timestamp=NOW, sample=None, quality_flags=[DataQualityFlag.MISSING]
    )
    assert vs.is_usable is False


def test_validated_sample_late_arrival_is_usable():
    s = TrafficSample(timestamp=NOW, output_id="OUT_001", traffic_mbps=100.0, loss_percent=0.1)
    vs = ValidatedSample(
        output_id="OUT_001",
        timestamp=NOW,
        sample=s,
        quality_flags=[DataQualityFlag.LATE_ARRIVAL],
    )
    assert vs.is_usable is True


# --- ForecastResult -----------------------------------------------------


def test_forecast_result_nominal():
    f = ForecastResult(
        output_id="OUT_002",
        timestamp=NOW,
        predicted_traffic_mbps=18100.0,
        lower_bound_mbps=16500.0,
        upper_bound_mbps=20000.0,
        confidence=0.9,
        model_name=ForecastModelName.HOLT_WINTERS,
    )
    assert f.interval_width_mbps == 3500.0


def test_forecast_result_rejects_inconsistent_bounds():
    with pytest.raises(ValidationError):
        ForecastResult(
            output_id="OUT_002",
            timestamp=NOW,
            predicted_traffic_mbps=25000.0,  # hors intervalle
            lower_bound_mbps=16500.0,
            upper_bound_mbps=20000.0,
            confidence=0.9,
            model_name=ForecastModelName.HOLT_WINTERS,
        )


# --- AnomalyResult -----------------------------------------------------


def test_anomaly_result_rejects_score_out_of_bounds():
    with pytest.raises(ValidationError):
        AnomalyResult(
            output_id="OUT_017",
            timestamp=NOW,
            traffic_anomaly_score=1.4,  # > 1
            loss_anomaly_score=0.9,
            joint_anomaly_score=0.9,
        )


# --- Incident / IncidentEvidence ----------------------------------------


def _sample_evidence(ts):
    return IncidentEvidence(
        timestamp=ts,
        traffic_mbps=8.1,
        expected_traffic_mbps=18.4,
        traffic_deviation_percent=-56.0,
        loss_percent=9.4,
        expected_loss_percent=0.2,
        traffic_anomaly_score=0.94,
        loss_anomaly_score=0.91,
    )


def test_incident_nominal_lifecycle():
    suspected = NOW
    opened = NOW + timedelta(minutes=10)
    last_seen = NOW + timedelta(minutes=25)

    inc = Incident(
        id="INC_001",
        output_id="OUT_017",
        status=IncidentStatus.OPEN,
        severity=Severity.CRITICAL,
        incident_type=IncidentType.TRAFFIC_DROP_WITH_HIGH_LOSS,
        suspected_at=suspected,
        opened_at=opened,
        last_seen_at=last_seen,
        score=0.94,
        peak_traffic_deviation_percent=-56.0,
        peak_loss_percent=9.4,
        evidence=[_sample_evidence(opened)],
    )
    assert inc.duration_seconds == 900.0  # 15 minutes entre opened_at et last_seen_at
    assert inc.is_active is True


def test_incident_rejects_opened_before_suspected():
    with pytest.raises(ValidationError):
        Incident(
            id="INC_002",
            output_id="OUT_017",
            status=IncidentStatus.OPEN,
            severity=Severity.WARNING,
            incident_type=IncidentType.TRAFFIC_DROP,
            suspected_at=NOW,
            opened_at=NOW - timedelta(minutes=5),  # incohérent
            last_seen_at=NOW,
            score=0.8,
        )


def test_incident_without_opened_at_has_no_duration():
    inc = Incident(
        id="INC_003",
        output_id="OUT_004",
        status=IncidentStatus.SUSPECT,
        severity=Severity.INFO,
        incident_type=IncidentType.TRAFFIC_DROP,
        suspected_at=NOW,
        last_seen_at=NOW,
        score=0.55,
    )
    assert inc.duration_seconds is None
    assert inc.is_active is True
