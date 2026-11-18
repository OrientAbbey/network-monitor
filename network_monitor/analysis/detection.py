"""
Détection d'anomalies.

Normalisation unique et partagée : score = min(1, écart / référence),
jamais un z-score classique — plus robuste aux queues épaisses et
directement interprétable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import ClassVar

import numpy as np

from network_monitor.analysis.features import compute_features, history_steps
from network_monitor.analysis.features_extended import (
    compute_extended_features,
)
from network_monitor.analysis.forecasting import SeasonalBaselineModel, compute_residual
from network_monitor.app.models import AnomalyResult, ForecastResult, TrafficSample

# Même quantile que l'intervalle de prévision : une bande "normale"
# à ~80% de couverture, pas un seuil à 1 dispersion (trop sensible au bruit).
_LOSS_BAND_Z = 1.2816


def normalized_score(deviation: float, reference: float) -> float:
    """Normalisation commune : borne l'écart entre 0 et 1."""
    if reference <= 0:
        return 1.0 if deviation != 0 else 0.0
    return float(min(1.0, abs(deviation) / reference))


@dataclass
class LossBaseline:
    """
    Baseline robuste des pertes pour une sortie : niveau habituel
    (médiane) et dispersion (MAD), avec un plancher pour éviter qu'une
    sortie très stable ne devienne hypersensible.
    """

    median_loss_percent: float
    dispersion: float  # déjà bornée par loss_std_floor

    @classmethod
    def from_history(cls, history: list[TrafficSample], loss_std_floor: float) -> LossBaseline:
        if not history:
            raise ValueError("Historique vide : impossible de calibrer la baseline des pertes")
        values = np.array([s.loss_percent for s in history])
        median = float(np.median(values))
        mad = float(np.median(np.abs(values - median))) * 1.4826
        return cls(median_loss_percent=median, dispersion=max(mad, loss_std_floor))


def detect_traffic_anomaly(sample: TrafficSample, forecast: ForecastResult) -> tuple[float, float, bool]:
    """
    Détection trafic : renvoie (score, écart_relatif_%, hors_intervalle).
    Une variation à l'intérieur de l'intervalle prédictif n'est pas une
    anomalie : le score n'est non nul qu'en dehors de l'intervalle.
    """
    residual = compute_residual(sample, forecast)
    if not residual.out_of_interval:
        return 0.0, residual.relative_residual * 100.0, False

    half_width = residual.interval_width_mbps / 2.0
    if sample.traffic_mbps < forecast.lower_bound_mbps:
        excess = forecast.lower_bound_mbps - sample.traffic_mbps
    else:
        excess = sample.traffic_mbps - forecast.upper_bound_mbps
    score = normalized_score(excess, half_width if half_width > 0 else forecast.predicted_traffic_mbps)
    return score, residual.relative_residual * 100.0, True


def detect_loss_anomaly(sample: TrafficSample, baseline: LossBaseline) -> tuple[float, float]:
    """
    Détection pertes : renvoie (score, écart en points de %).

    Même logique que le trafic : une déviation à l'intérieur d'une
    bande "normale" (~80% de couverture, cohérent avec la confiance retenue
    pour le forecasting) n'est pas une anomalie. Score nul sous la baseline et dans
    cette bande, croissant ensuite avec l'excès — pas un ratio brut, qui
    saturerait à 1.0 dès qu'un simple bruit dépasse 1 dispersion.
    """
    deviation = sample.loss_percent - baseline.median_loss_percent
    normal_band = _LOSS_BAND_Z * baseline.dispersion
    # Seule une *hausse* des pertes est une anomalie.
    # Des pertes inférieures à l'habitude ne le sont jamais : sur une sortie stable, un
    # bruit qui écrête les pertes à 0 % passait sinon pour un incident sévère.
    if deviation <= normal_band:
        return 0.0, deviation
    score = normalized_score(deviation - normal_band, normal_band)
    return score, deviation


@dataclass
class PersistenceTracker:
    """
    Suit, par sortie, le nombre de pas consécutifs où le score combiné
    dépasse un seuil, pour alimenter l'hystérésis du moteur
    d'incidents.
    """

    interval_seconds: float
    _consecutive_steps: dict[str, int] = field(default_factory=dict)

    def update(self, output_id: str, joint_score: float, threshold: float) -> float:
        """Met à jour le compteur et renvoie la durée persistante en secondes."""
        if joint_score >= threshold:
            self._consecutive_steps[output_id] = self._consecutive_steps.get(output_id, 0) + 1
        else:
            self._consecutive_steps[output_id] = 0
        return self._consecutive_steps[output_id] * self.interval_seconds


def combine_scores(traffic_score: float, loss_score: float) -> float:
    """
    Score combiné : le maximum domine, mais la co-occurrence
    renforce la confiance (trafic↓↓↓ + pertes↑↑↑) sans jamais
    dépasser 1 ni supprimer les scores individuels.
    """
    base = max(traffic_score, loss_score)
    both_significant = min(traffic_score, loss_score) > 0.3
    boost = 0.15 * min(traffic_score, loss_score) if both_significant else 0.0
    return float(min(1.0, base + boost))


def build_anomaly_result(
    sample: TrafficSample,
    forecast: ForecastResult,
    loss_baseline: LossBaseline,
    persistence: PersistenceTracker,
    open_threshold: float,
    data_gap: bool = False,
    loss_enabled: bool = True,
) -> AnomalyResult:
    """Assemble un AnomalyResult complet à partir d'une observation validée.

    `loss_enabled=False` (configuration `detection.loss.enabled`) désactive
    la composante pertes : seule la détection trafic contribue au score.
    """
    traffic_score, traffic_deviation_pct, out_of_interval = detect_traffic_anomaly(sample, forecast)
    if loss_enabled:
        loss_score, loss_deviation_pts = detect_loss_anomaly(sample, loss_baseline)
    else:
        loss_score, loss_deviation_pts = 0.0, None
    joint_score = combine_scores(traffic_score, loss_score)

    duration_seconds = persistence.update(sample.output_id, joint_score, open_threshold)
    max_persistence_seconds = 3600.0  # au-delà d'1h, la persistance est considérée maximale
    persistence_score = float(min(1.0, duration_seconds / max_persistence_seconds))

    return AnomalyResult(
        output_id=sample.output_id,
        timestamp=sample.timestamp,
        traffic_anomaly_score=traffic_score,
        loss_anomaly_score=loss_score,
        persistence_score=persistence_score,
        joint_anomaly_score=joint_score,
        traffic_deviation_percent=traffic_deviation_pct,
        loss_deviation_points=loss_deviation_pts,
        traffic_out_of_interval=out_of_interval,
        data_gap=data_gap,
    )


class IsolationForestDetector:
    """
    Détecteur optionnel de confirmation, propre à une sortie.

    Indépendant des règles statistiques : il apprend, sur l'historique, à
    isoler les combinaisons inhabituelles de features contextualisées par
    rapport à sa *propre* baseline saisonnière. Son score n'entre dans aucun
    score de détection ni dans aucune décision d'incident : il ne fait
    qu'enrichir les preuves (`IncidentEvidence.isolation_score`).

    Deux forêts, une par forme d'anomalie, et le score est le plus élevé des deux :
    - « niveau » (écart à l'attendu, pertes, moyenne sur 1 h) : un palier stable
      (chute soutenue) y est très isolé ;
    - « dynamique » (variations sur 15 min / 1 h, volatilité) : une transition
      brutale, une rampe, des à-coups.
    Une seule forêt sur toutes les dimensions diluait les paliers : un palier n'a
    qu'une ou deux dimensions inhabituelles, que les coupes aléatoires ignoraient
    le plus souvent, et son score retombait à zéro dès que les variations cessaient.

    Les features sont rapportées au trafic attendu (sans dimension) : des Mbps bruts
    varieraient avec le cycle journalier et noieraient le signal. Chaque score est
    calibré sur l'entraînement : 0 tant que l'observation est aussi habituelle que
    99 % de l'historique, 1 au-delà de son pire cas.
    """

    _MAX_TRAINING_ROWS = 20_000
    _RELATIVE: ClassVar[frozenset[str]] = frozenset(
        {"rolling_std_1h", "traffic_change_15m", "traffic_change_1h", "rolling_mean_1h"}
    )
    _GROUPS: ClassVar[dict[str, tuple[str, ...]]] = {
        "level": ("traffic_vs_expected", "loss_vs_expected", "rolling_mean_1h"),
        "dynamics": ("rolling_std_1h", "traffic_change_15m", "traffic_change_1h", "loss_change_15m"),
    }
    _EXTRAPOLATION = 0.5
    """Une forêt d'isolement extrapole mal : une valeur loin au-delà de tout ce qu'elle a vu
    n'est pas plus isolée qu'une valeur à peine au-delà. Au-delà de `_EXTRAPOLATION` fois
    l'étendue d'entraînement d'une feature, l'observation est donc déclarée maximalement
    inhabituelle (score 1) — c'est exactement ce que « pire que tout l'historique » veut dire."""

    def __init__(self, output_id: str, interval: timedelta, n_estimators: int = 100) -> None:
        self._interval = interval
        self._n_estimators = n_estimators
        self._baseline = SeasonalBaselineModel(output_id)
        self._median_loss = 0.0
        self._forests: dict[str, object] = {}
        self._q99: dict[str, float] = {}
        self._worst: dict[str, float] = {}
        self._bounds: dict[str, tuple[float, float]] = {}

    def _features(self, sample: TrafficSample, expected_traffic: float, recent) -> dict[str, float] | None:
        ext = compute_extended_features(sample, recent, self._interval)
        if ext is None:
            return None
        feats = compute_features(sample, expected_traffic, self._median_loss, recent) | ext
        scale = expected_traffic if expected_traffic > 0 else 1.0
        return {k: v / scale if k in self._RELATIVE else v for k, v in feats.items()}

    def fit(self, history: list[TrafficSample]) -> None:
        try:
            from sklearn.ensemble import IsolationForest
        except ImportError as exc:  # pragma: no cover - dépendance optionnelle
            raise ImportError(
                "detection.isolation_forest.enabled nécessite scikit-learn (pip install scikit-learn)"
            ) from exc

        history = sorted(history, key=lambda s: s.timestamp)
        self._baseline.fit(history)
        self._median_loss = float(np.median([s.loss_percent for s in history]))
        expected = {
            f.timestamp: f.predicted_traffic_mbps
            for f in self._baseline.predict(history[0].timestamp - self._interval, self._interval * len(history), self._interval)
        }
        recent: deque[TrafficSample] = deque(maxlen=history_steps(self._interval))
        rows: list[dict[str, float]] = []
        for sample in history:
            exp = expected.get(sample.timestamp)
            if exp is not None:
                feats = self._features(sample, exp, recent)
                if feats is not None:
                    rows.append(feats)
            recent.append(sample)
        if len(rows) < 100:
            raise ValueError("Historique insuffisant pour entraîner l'Isolation Forest")
        rows = rows[-self._MAX_TRAINING_ROWS:]
        for name, keys in self._GROUPS.items():
            train = np.array([[r[k] for k in keys] for r in rows])
            forest = IsolationForest(n_estimators=self._n_estimators, max_samples=min(1024, len(train)), random_state=0)
            forest.fit(train)
            raw = -forest.score_samples(train)  # ~0,5 habituel, → 1 isolé
            self._q99[name] = float(np.quantile(raw, 0.99))
            self._worst[name] = max(float(raw.max()), self._q99[name] + 0.05)
            self._forests[name] = forest
            for i, k in enumerate(keys):
                self._bounds[k] = (float(train[:, i].min()), float(train[:, i].max()))

    def _beyond_range(self, key: str, value: float) -> bool:
        low, high = self._bounds[key]
        margin = self._EXTRAPOLATION * max(high - low, 1e-9)
        return value < low - margin or value > high + margin

    def score(self, sample: TrafficSample, recent) -> float | None:
        """Score dans [0, 1] (0 = habituel, 1 = pire que tout l'historique), ou
        None si le tampon `recent` ne couvre pas encore une heure complète."""
        if not self._forests:
            return None
        expected = self._baseline.predict(sample.timestamp - self._interval, self._interval, self._interval)[0]
        feats = self._features(sample, expected.predicted_traffic_mbps, recent)
        if feats is None:
            return None
        scores = []
        for name, keys in self._GROUPS.items():
            raw = float(-self._forests[name].score_samples(np.array([[feats[k] for k in keys]]))[0])  # type: ignore[attr-defined]
            score = (raw - self._q99[name]) / (self._worst[name] - self._q99[name])
            if any(self._beyond_range(k, feats[k]) for k in keys):
                score = 1.0
            scores.append(score)
        return float(min(1.0, max(0.0, max(scores))))
