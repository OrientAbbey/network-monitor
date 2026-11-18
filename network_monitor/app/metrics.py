"""Format texte Prometheus (exposition 0.0.4), écrit à la main : pas de dépendance pour une trentaine de lignes.

Chaque famille est un quadruplet (nom, aide, type, échantillons) ; un échantillon est (étiquettes, valeur).
Les valeurs d'étiquettes sont échappées (antislash, guillemet, saut de ligne), sans quoi un identifiant de sortie
contenant l'un d'eux rendrait toute la page illisible pour Prometheus."""

from __future__ import annotations

import math

Sample = tuple[dict[str, str], float]
Family = tuple[str, str, str, list[Sample]]


def _escape(text: str, *, label: bool = False) -> str:
    text = text.replace("\\", "\\\\").replace("\n", "\\n")
    return text.replace('"', '\\"') if label else text


def _number(value: float) -> str:
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if value.is_integer() else repr(value)


def render(families: list[Family]) -> str:
    lines: list[str] = []
    for name, help_text, kind, samples in families:
        if not samples:
            continue
        lines += [f"# HELP {name} {_escape(help_text)}", f"# TYPE {name} {kind}"]
        for labels, value in samples:
            tag = "{" + ",".join(f'{k}="{_escape(v, label=True)}"' for k, v in labels.items()) + "}" if labels else ""
            lines.append(f"{name}{tag} {_number(value)}")
    return "\n".join(lines) + "\n"
