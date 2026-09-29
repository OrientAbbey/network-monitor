"""Ligne de commande : `python -m network_monitor.evaluation [--seed N] [--days D] [--json] [--out FICHIER]`."""

from __future__ import annotations

import argparse
import json
import sys

from network_monitor.evaluation.harness import run_evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m network_monitor.evaluation",
        description="Évalue détection et prévision sur le Mock (§33), sur une période distincte de la calibration.",
    )
    parser.add_argument("--seed", type=int, default=42, help="graine du Mock (défaut : 42)")
    parser.add_argument("--days", type=float, default=2.0, help="durée de la période d'évaluation, en jours (défaut : 2)")
    parser.add_argument("--json", action="store_true", help="sortie JSON au lieu du texte")
    parser.add_argument("--out", help="écrire le rapport JSON dans ce fichier")
    args = parser.parse_args(argv)

    report = run_evaluation(seed=args.seed, eval_days=args.days)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, ensure_ascii=False)
    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) if args.json else report.format_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
