"""
Évaluation quantitative (§33) : détections et prévisions comparées à la vérité terrain du Mock,
sur une période distincte de la calibration.

`metrics` : fonctions pures. `harness` : exécute le pipeline complet sur une suite de scénarios.
Ligne de commande : `python -m network_monitor.evaluation --help`.
"""

from network_monitor.evaluation.harness import EvaluationReport, default_scenarios, run_evaluation
from network_monitor.evaluation.metrics import (
    false_positive_rate,
    forecast_metrics,
    match_incidents,
    summarize_delays,
)

__all__ = [
    "EvaluationReport",
    "default_scenarios",
    "false_positive_rate",
    "forecast_metrics",
    "match_incidents",
    "run_evaluation",
    "summarize_delays",
]
