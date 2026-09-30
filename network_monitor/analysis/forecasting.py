"""
Forecasting (§10-§13) : interface commune + deux implémentations V1.

Holt-Winters ne porte que la saisonnalité quotidienne (§10.B) ; l'effet
hebdomadaire est porté par la Seasonal Baseline. Le trafic est modélisé en
interne via log1p (§9.3) — jamais exposé tel quel en dehors de ce module.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from network_monitor.app.models import ForecastModelName, ForecastResult, TrafficSample

logger = logging.getLogger(__name__)

# Quantile normal à 80% bilatéral, utilisé pour dimensionner l'intervalle
# prédictif (§13). Valeur fixe et documentée plutôt qu'un choix implicite.
_Z_80 = 1.2816
_FORECAST_CONFIDENCE = 0.80


@dataclass(frozen=True)
class ResidualReport:
    """Écart entre une observation et sa prévision (§14)."""

    residual_mbps: float
    relative_residual: float
    out_of_interval: bool
    interval_width_mbps: float


def compute_residual(sample: TrafficSample, forecast: ForecastResult) -> ResidualReport:
    """Calcule le résidu et l'écart relatif d'une observation par rapport à sa prévision (§14)."""
    residual = sample.traffic_mbps - forecast.predicted_traffic_mbps
    relative = residual / forecast.predicted_traffic_mbps if forecast.predicted_traffic_mbps > 0 else 0.0
    out_of_interval = not (forecast.lower_bound_mbps <= sample.traffic_mbps <= forecast.upper_bound_mbps)
    return ResidualReport(
        residual_mbps=residual,
        relative_residual=relative,
        out_of_interval=out_of_interval,
        interval_width_mbps=forecast.interval_width_mbps,
    )


class ForecastModel(ABC):
    """Interface commune à tout modèle de prévision (§36 : remplaçable sans toucher au reste)."""

    name: ForecastModelName

    @abstractmethod
    def fit(self, history: list[TrafficSample]) -> None:
        """Calibre le modèle sur l'historique d'une seule sortie."""
        raise NotImplementedError

    @abstractmethod
    def predict(self, from_time: datetime, horizon: timedelta, interval: timedelta) -> list[ForecastResult]:
        """Produit une prévision par pas de `interval` jusqu'à `horizon` après `from_time`."""
        raise NotImplementedError


class SeasonalBaselineModel(ForecastModel):
    """
    Référence par (jour de semaine, créneau), avec créneaux voisins inclus
    pour rester robuste avec peu d'historique (§10.A). Médiane privilégiée
    à la moyenne.
    """

    name = ForecastModelName.SEASONAL_BASELINE

    def __init__(self, output_id: str, neighbor_slots: int = 3) -> None:
        self.output_id = output_id
        self._neighbor_slots = neighbor_slots
        self._interval: timedelta | None = None
        # (jour_de_semaine, index_du_créneau) -> valeurs de trafic observées
        self._buckets: dict[tuple[int, int], list[float]] = {}

    def fit(self, history: list[TrafficSample]) -> None:
        if not history:
            raise ValueError("Historique vide : impossible de calibrer la baseline saisonnière")
        history = sorted(history, key=lambda s: s.timestamp)
        self._interval = _infer_interval(history)
        slots_per_day = _slots_per_day(self._interval)

        buckets: dict[tuple[int, int], list[float]] = {}
        for sample in history:
            slot = _slot_index(sample.timestamp, self._interval)
            buckets.setdefault((sample.timestamp.weekday(), slot), []).append(sample.traffic_mbps)
        self._buckets = buckets
        self._slots_per_day = slots_per_day
        # Dispersion relative globale (§10.A) : avec peu d'historique, un
        # bucket isolé (peu d'occurrences du même jour/créneau) peut
        # sous-estimer sa vraie variabilité par pur effet d'échantillonnage.
        # Ce plancher, mesuré sur l'ensemble des buckets, stabilise
        # l'estimation locale sans effacer les écarts de dispersion réels
        # entre créneaux (heures de pointe vs creuses, etc.).
        self._global_relative_mad = self._compute_global_relative_mad(buckets)

    @staticmethod
    def _compute_global_relative_mad(buckets: dict[tuple[int, int], list[float]]) -> float:
        relative_deviations: list[float] = []
        for values in buckets.values():
            if len(values) < 2:
                continue
            median = float(np.median(values))
            if median <= 0:
                continue
            relative_deviations.extend(np.abs(np.array(values) / median - 1.0))
        if not relative_deviations:
            return 0.05  # repli raisonnable si l'historique est trop pauvre pour être mesuré
        return float(np.median(relative_deviations)) * 1.4826

    def predict(self, from_time: datetime, horizon: timedelta, interval: timedelta) -> list[ForecastResult]:
        if not self._buckets:
            raise RuntimeError("Modèle non calibré : appeler fit() avant predict()")

        results = []
        n_steps = max(1, int(horizon / interval))
        for i in range(1, n_steps + 1):
            ts = from_time + i * interval
            values = self._values_near(ts, interval)
            if not values:
                # Aucune référence historique pour ce créneau : prévision
                # non informative centrée sur zéro plutôt qu'un plantage.
                results.append(
                    ForecastResult(
                        output_id=self.output_id,
                        timestamp=ts,
                        predicted_traffic_mbps=0.0,
                        lower_bound_mbps=0.0,
                        upper_bound_mbps=0.0,
                        confidence=0.0,
                        model_name=self.name,
                    )
                )
                continue

            median = float(np.median(values))
            bucket_mad = float(np.median(np.abs(np.array(values) - median))) * 1.4826  # équivalent écart-type robuste
            mad = max(bucket_mad, self._global_relative_mad * median, 0.01 * median, 0.5)
            lower = max(0.0, median - _Z_80 * mad)
            upper = median + _Z_80 * mad
            results.append(
                ForecastResult(
                    output_id=self.output_id,
                    timestamp=ts,
                    predicted_traffic_mbps=median,
                    lower_bound_mbps=lower,
                    upper_bound_mbps=upper,
                    confidence=_FORECAST_CONFIDENCE,
                    model_name=self.name,
                )
            )
        return results

    def _values_near(self, ts: datetime, interval: timedelta) -> list[float]:
        slot = _slot_index(ts, self._interval or interval)
        weekday = ts.weekday()
        values: list[float] = []
        for offset in range(-self._neighbor_slots, self._neighbor_slots + 1):
            key = (weekday, (slot + offset) % self._slots_per_day)
            values.extend(self._buckets.get(key, []))
        return values


class HoltWintersModel(ForecastModel):
    """
    Holt-Winters additif en espace log1p (§9.3, §10.B). Ne porte que la
    saisonnalité quotidienne : `seasonal_periods` = nombre de points par jour.
    """

    name = ForecastModelName.HOLT_WINTERS

    def __init__(self, output_id: str) -> None:
        self.output_id = output_id
        self._fit_result: Any = None
        self._interval: timedelta | None = None
        self._residual_std_log: float = 0.0
        self._last_timestamp: datetime | None = None

    def fit(self, history: list[TrafficSample]) -> None:
        history = sorted(history, key=lambda s: s.timestamp)
        self._last_timestamp = history[-1].timestamp
        self._interval = _infer_interval(history)
        seasonal_periods = _slots_per_day(self._interval)

        if len(history) < 2 * seasonal_periods:
            raise ValueError(
                f"Historique insuffisant pour Holt-Winters : {len(history)} points, "
                f"{2 * seasonal_periods} requis (2 cycles quotidiens minimum)"
            )

        series = np.log1p(_regular_series(history, self._interval))
        model = ExponentialSmoothing(
            series,
            trend="add",
            damped_trend=True,
            seasonal="add",
            seasonal_periods=seasonal_periods,
        )
        self._fit_result = model.fit(optimized=True)
        retvals = getattr(self._fit_result, "mle_retvals", None)
        if retvals is not None and not getattr(retvals, "success", True):
            # L'ajustement reste utilisable ; c'est le backtest de select_model qui départage les modèles.
            logger.warning("Holt-Winters : l'optimisation n'a pas convergé pour %s", self.output_id)

        residuals = series - self._fit_result.fittedvalues
        self._residual_std_log = float(np.std(residuals))

    def predict(self, from_time: datetime, horizon: timedelta, interval: timedelta) -> list[ForecastResult]:
        if self._fit_result is None or self._last_timestamp is None:
            raise RuntimeError("Modèle non calibré : appeler fit() avant predict()")

        n_steps = max(1, int(horizon / interval))

        # statsmodels prévoit toujours depuis la fin de la série d'entraînement : on convertit
        # l'horodatage demandé en nombre de pas depuis cette fin.
        first_step = round((from_time + interval - self._last_timestamp) / interval)
        if first_step < 1:
            raise ValueError(
                f"from_time ({from_time}) antérieur à la fin de l'historique d'entraînement "
                f"({self._last_timestamp}) : Holt-Winters ne prévoit qu'au-delà de sa série d'entraînement"
            )
        last_step = first_step + n_steps - 1
        forecast_log = self._fit_result.forecast(last_step)[first_step - 1 :]

        results = []
        for i, log_pred in enumerate(forecast_log, start=1):
            ts = from_time + i * interval
            steps_from_fit_end = first_step + i - 1
            # L'incertitude croît avec la racine de la distance à la fin de l'entraînement.
            spread = _Z_80 * self._residual_std_log * math.sqrt(steps_from_fit_end)
            predicted = float(np.expm1(log_pred))
            lower = float(np.expm1(log_pred - spread))
            upper = float(np.expm1(log_pred + spread))
            results.append(
                ForecastResult(
                    output_id=self.output_id,
                    timestamp=ts,
                    predicted_traffic_mbps=max(0.0, predicted),
                    lower_bound_mbps=max(0.0, min(lower, predicted)),
                    upper_bound_mbps=max(upper, predicted),
                    confidence=_FORECAST_CONFIDENCE,
                    model_name=self.name,
                )
            )
        return results


# ---------------------------------------------------------------------------
# Sélection de modèle (§11)
# ---------------------------------------------------------------------------


def select_model(
    output_id: str,
    history: list[TrafficSample],
    minimum_history_for_selection: timedelta,
    validation_window: timedelta,
    allowed_models: list[str] | None = None,
) -> ForecastModel:
    """
    Choisit le meilleur modèle par backtesting simple (erreur absolue sur
    la fenêtre de validation), avec repli automatique sur la baseline
    saisonnière si l'historique est trop court (§11).

    `allowed_models` (config `forecast.models`, §35) restreint les candidats ;
    `None` = tous. La baseline saisonnière reste le repli universel même si
    elle n'est pas listée : sans modèle, le système ne peut pas fonctionner.
    """
    allowed = set(allowed_models) if allowed_models is not None else {"seasonal_baseline", "holt_winters"}
    if not history:
        raise ValueError("Historique vide : impossible de sélectionner un modèle")

    history = sorted(history, key=lambda s: s.timestamp)
    span = history[-1].timestamp - history[0].timestamp

    baseline = SeasonalBaselineModel(output_id)
    if span < minimum_history_for_selection:
        baseline.fit(history)
        return baseline

    cutoff = history[-1].timestamp - validation_window
    train = [s for s in history if s.timestamp <= cutoff]
    validation = [s for s in history if s.timestamp > cutoff]
    if not train or not validation:
        baseline.fit(history)
        return baseline

    interval = _infer_interval(history)
    candidates: list[ForecastModel] = []
    if "seasonal_baseline" in allowed:
        candidates.append(SeasonalBaselineModel(output_id))
    if "holt_winters" in allowed:
        candidates.append(HoltWintersModel(output_id))
    if not candidates:
        baseline.fit(history)
        return baseline

    best_model: ForecastModel | None = None
    best_mae = math.inf
    for candidate in candidates:
        try:
            candidate.fit(train)
            mae = _backtest_mae(candidate, train, validation, interval)
        except Exception:
            continue
        if mae < best_mae:
            best_mae = mae
            best_model = candidate

    if best_model is None:
        baseline.fit(history)
        return baseline

    # Le modèle retenu est refit sur l'historique complet avant usage réel.
    best_model.fit(history)
    return best_model


def _backtest_mae(
    model: ForecastModel, train: list[TrafficSample], validation: list[TrafficSample], interval: timedelta
) -> float:
    horizon = interval * len(validation)
    forecasts = model.predict(train[-1].timestamp, horizon, interval)
    forecast_by_ts = {f.timestamp: f for f in forecasts}
    errors = [
        abs(sample.traffic_mbps - forecast_by_ts[sample.timestamp].predicted_traffic_mbps)
        for sample in validation
        if sample.timestamp in forecast_by_ts
    ]
    return float(np.mean(errors)) if errors else math.inf


# ---------------------------------------------------------------------------
# Utilitaires internes
# ---------------------------------------------------------------------------


def _infer_interval(history: list[TrafficSample]) -> timedelta:
    """Pas d'échantillonnage : médiane des écarts entre points consécutifs (insensible aux trous)."""
    deltas = sorted(
        (history[i].timestamp - history[i - 1].timestamp).total_seconds()
        for i in range(1, min(len(history), 501))
        if history[i].timestamp > history[i - 1].timestamp
    )
    if not deltas:
        return timedelta(minutes=5)  # valeur par défaut (§3) si un seul point
    return timedelta(seconds=deltas[len(deltas) // 2])


def _regular_series(history: list[TrafficSample], interval: timedelta) -> np.ndarray:
    """
    Trafic sur une grille régulière, créneaux absents interpolés linéairement. Holt-Winters exige une
    série sans trou : un créneau manquant décalerait toute la saisonnalité qui suit.
    """
    t0 = history[0].timestamp
    n = int(round((history[-1].timestamp - t0) / interval)) + 1
    values = np.full(n, np.nan)
    for sample in history:
        idx = int(round((sample.timestamp - t0) / interval))
        if 0 <= idx < n:
            values[idx] = sample.traffic_mbps
    missing = np.isnan(values)
    if missing.any():
        values[missing] = np.interp(np.flatnonzero(missing), np.flatnonzero(~missing), values[~missing])
    return values


def _slots_per_day(interval: timedelta) -> int:
    return max(1, int(timedelta(days=1) / interval))


def _slot_index(ts: datetime, interval: timedelta) -> int:
    minutes_since_midnight = ts.hour * 60 + ts.minute
    interval_minutes = max(1, int(interval.total_seconds() // 60))
    return minutes_since_midnight // interval_minutes
