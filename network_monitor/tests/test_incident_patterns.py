"""Motifs d'incident (§22-§23) : intermittent = un seul incident, progressif, brusque, bruit isolé."""

from datetime import datetime, timedelta, timezone

from network_monitor.analysis.detection import LossBaseline, detect_loss_anomaly
from network_monitor.app.config import IncidentConfig
from network_monitor.app.models import (
    AnomalyResult,
    IncidentStatus,
    IncidentType,
    TrafficSample,
)
from network_monitor.incidents.manager import IncidentManager

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


def _run(deviations: list[float], **overrides):
    """Rejoue un écart de trafic (en %) par pas ; le score en est déduit comme le fait la détection."""
    manager = IncidentManager(IncidentConfig(**({"minimum_duration": "15m"} | overrides)))
    touched = {}
    for i, dev in enumerate(deviations):
        score = min(1.0, max(0.0, (abs(dev) - 15.0) / 20.0))  # 15 % : bande normale ; 35 % et + : saturé
        result = AnomalyResult(
            output_id="O", timestamp=T0 + i * STEP, traffic_anomaly_score=score, loss_anomaly_score=0.0,
            joint_anomaly_score=score, traffic_deviation_percent=dev,
        )
        _, incident = manager.process(
            result, expected_traffic_mbps=100.0, observed_traffic_mbps=100.0 * (1 + dev / 100), observed_loss_percent=0.1
        )
        if incident is not None:
            touched[incident.id] = incident
    return [i for i in touched.values() if i.opened_at is not None]


def test_intermittent_incident_stays_one_incident_and_is_typed():
    # 20 min de période, actif 15 min sur 20 (trois pas « actifs », un pas de répit)
    incidents = _run(([-60.0, -60.0, -60.0, 0.0] * 6) + [0.0] * 4)
    assert len(incidents) == 1
    assert incidents[0].incident_type == IncidentType.INTERMITTENT_DEGRADATION
    assert incidents[0].status == IncidentStatus.CLOSED


def test_sparse_intermittent_is_noise_by_default_and_detectable_when_configured():
    """Un seul pas « actif » sur trois n'est pas distinguable du bruit au pas de 5 min :
    ignoré par défaut (`min_suspect_steps=3`), repérable en abaissant ce réglage et en allongeant `recovery_duration` au-delà de
    deux pas de calme (au prix de faux incidents)."""
    sparse = ([-60.0, 0.0, 0.0] * 10) + [0.0] * 4
    assert _run(sparse) == []
    incidents = _run(sparse, min_suspect_steps=1, recovery_duration="10m")
    assert len(incidents) == 1 and incidents[0].incident_type == IncidentType.INTERMITTENT_DEGRADATION


def test_progressive_degradation_is_typed():
    ramp = [-(20 + 40 * i / 17) for i in range(18)]  # -20 % → -60 % en 90 min
    incidents = _run(ramp + [0.0] * 4)
    assert len(incidents) == 1 and incidents[0].incident_type == IncidentType.PROGRESSIVE_DEGRADATION


def test_abrupt_drop_is_not_progressive():
    incidents = _run([-60.0] * 12 + [0.0] * 4)
    assert len(incidents) == 1 and incidents[0].incident_type == IncidentType.TRAFFIC_DROP


def test_recovery_does_not_change_the_type():
    """La rampe est suivie d'un retour brutal à la normale : le type reste « progressif »."""
    ramp = [-(20 + 40 * i / 17) for i in range(18)]
    assert _run(ramp + [-5.0, 0.0, 0.0, 0.0, 0.0])[0].incident_type == IncidentType.PROGRESSIVE_DEGRADATION


def test_isolated_spikes_never_open_an_incident():
    # un pic d'un pas toutes les 12 pas : ni tolérance ni ouverture
    assert _run(([-60.0] + [0.0] * 11) * 8) == []


def test_two_step_burst_followed_by_calm_does_not_open():
    assert _run([-60.0, -60.0] + [0.0] * 30) == []


def test_loss_below_baseline_is_never_an_anomaly():
    """Des pertes inférieures à l'habitude (bruit écrêté à 0 %) ne sont pas un incident."""
    baseline = LossBaseline(median_loss_percent=0.15, dispersion=0.05)
    sample = TrafficSample(timestamp=T0, output_id="O", traffic_mbps=100.0, loss_percent=0.0)
    score, deviation = detect_loss_anomaly(sample, baseline)
    assert score == 0.0 and deviation < 0
    high = sample.model_copy(update={"loss_percent": 0.6})
    assert detect_loss_anomaly(high, baseline)[0] > 0.5
