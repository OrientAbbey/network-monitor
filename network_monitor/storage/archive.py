"""
Archive Parquet des mesures brutes : ce que la purge retire de la base n'est pas perdu,
il est rangé en colonnes compressées — un dossier par sortie, un fichier par jour. Relisible
telle quelle par `ParquetDataSource` (rejeu, contre-expertise d'un incident, évaluation).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timezone
from pathlib import Path
from urllib.parse import quote

import polars as pl

from network_monitor.app.models import TrafficSample

_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "timestamp": pl.Datetime("us", "UTC"),
    "output_id": pl.Utf8,
    "traffic_mbps": pl.Float64,
    "loss_percent": pl.Float64,
}


class ParquetArchive:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _file(self, output_id: str, day: str) -> Path:
        # Le nom de dossier est l'identifiant encodé : une sortie « a/b » ne sort pas de la racine.
        return self.root / quote(output_id, safe="") / f"{day}.parquet"

    def write(self, samples: Iterable[TrafficSample]) -> int:
        """Ajoute les mesures à l'archive (un fichier par sortie et par jour). Idempotent : une
        mesure déjà archivée (même sortie, même instant) est remplacée, pas dupliquée."""
        rows = [
            {"timestamp": s.timestamp.astimezone(timezone.utc), "output_id": s.output_id,
             "traffic_mbps": s.traffic_mbps, "loss_percent": s.loss_percent}
            for s in samples
        ]
        if not rows:
            return 0
        df = pl.DataFrame(rows, schema=_SCHEMA)
        df = df.with_columns(pl.col("timestamp").dt.strftime("%Y-%m-%d").alias("_day"))
        for (output_id, day), part in df.group_by(["output_id", "_day"], maintain_order=True):
            path = self._file(str(output_id), str(day))
            part = part.drop("_day")
            if path.exists():
                part = pl.concat([pl.read_parquet(path), part]).unique(["timestamp", "output_id"], keep="last")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            part.sort("timestamp").write_parquet(tmp, compression="zstd")
            tmp.replace(path)  # écriture atomique : jamais de fichier à moitié écrit
        return len(rows)
