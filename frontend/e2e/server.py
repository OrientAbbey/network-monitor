"""
Serveur de test pour Playwright : le **vrai** backend (boucle, détection, base SQLite, API), servant
l'interface compilée sur la même origine — exactement le montage de l'image Docker. Seules différences
avec la démonstration : un incident déterministe est injecté, la boucle va 8× plus vite que la
démonstration (0,25 s par pas de 5 min) et un utilisateur de test existe (alice / e2e-pass).

    python frontend/e2e/server.py <port> <dossier-temporaire>
"""

import json
import os
import sys
from pathlib import Path

port, workdir = int(sys.argv[1]), Path(sys.argv[2])
workdir.mkdir(parents=True, exist_ok=True)
frontend = Path(__file__).resolve().parent.parent

# L'environnement se règle AVANT l'import de l'API : le montage de l'interface et la configuration sont lus à l'import.
(workdir / "config.yaml").write_text(
    f"history: {{minimum: 30d, preferred: 40d}}\nstorage: {{database_url: 'sqlite:///{workdir / 'e2e.db'}'}}\n", encoding="utf-8"
)
os.environ.update(NM_CONFIG=str(workdir / "config.yaml"), NM_STATIC_DIR=str(frontend / "dist-e2e"), NM_AUTH_SECRET="e2e-secret")

import uvicorn  # noqa: E402

from network_monitor.app import api, auth  # noqa: E402
from network_monitor.app.models import Severity  # noqa: E402
from network_monitor.data.mock import AnomalyScenario, ScenarioType  # noqa: E402

os.environ["NM_USERS"] = json.dumps({"alice": auth.hash_password("e2e-pass")})

state = api.state
state.seconds_per_tick = 0.25
step = state.source.get_interval()
state.source.add_scenario(  # chute de trafic de 3 h 50 simulées, qui démarre 40 minutes simulées après le lancement
    AnomalyScenario(
        scenario_id="E2E", output_id="OUT_001", start_time=state.now + 8 * step, end_time=state.now + 54 * step,
        type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL, traffic_drop_fraction=0.7,
    )
)
uvicorn.run(api.app, host="127.0.0.1", port=port, log_level="warning")
