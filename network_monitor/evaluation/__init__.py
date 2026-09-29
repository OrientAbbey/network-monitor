"""
Évaluation quantitative (§33) : compare ce que le système a détecté et prévu à
la vérité terrain du Mock (`MockDataSource.ground_truth()`), sur une période
DISTINCTE de celle utilisée pour la calibration.

- `metrics` : fonctions pures (appariement scénarios/incidents, précision,
  rappel, F1, taux de faux positifs, délais de détection/fermeture, erreur de
  durée ; MAE, RMSE, sMAPE, couverture de l'intervalle prédictif).
- `harness` : exécute le pipeline complet sur une suite de scénarios et
  produit un `EvaluationReport`.

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
