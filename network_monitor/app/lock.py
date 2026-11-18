"""
Verrou d'instance unique : la boucle de monitoring vit **dans** le processus de l'API. Deux
processus (par ex. `uvicorn --workers 2`) lanceraient deux boucles qui calibrent, détectent et
alertent chacune de leur côté sur la même base : doubles alertes, incidents dédoublés.
Le verrou est un verrou de fichier du système (relâché par le noyau si le processus meurt, même
brutalement : aucun fichier « orphelin » ne bloque un redémarrage).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO

if sys.platform == "win32":  # pragma: no cover - exercé sous Windows seulement
    import msvcrt
else:
    import fcntl


class AlreadyRunningError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: IO[bytes] | None = None

    def acquire(self) -> None:
        workers = os.environ.get("WEB_CONCURRENCY", "1")
        if workers.isdigit() and int(workers) > 1:
            raise AlreadyRunningError(
                f"WEB_CONCURRENCY={workers} : l'application doit tourner avec un seul worker "
                "(la boucle de monitoring est dans le processus). Lancer avec --workers 1."
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if sys.platform == "win32":  # pragma: no cover
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AlreadyRunningError(
                f"Une autre instance détient déjà {self.path} : la boucle de monitoring ne doit tourner "
                "qu'une fois (lancer avec --workers 1, et un seul conteneur ou processus par base)."
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if sys.platform == "win32":  # pragma: no cover
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
