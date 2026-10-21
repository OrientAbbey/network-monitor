"""
Serveur de test pour Playwright : le **vrai** backend (boucle, détection, base SQLite, API), servant
l'interface compilée sur la même origine — exactement le montage de l'image Docker. Seules différences
avec la démonstration : deux incidents déterministes sont injectés (OUT_002 est déclarée en aval de OUT_001), la boucle va 8× plus vite que la
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
    f"history: {{minimum: 30d, preferred: 40d}}\nstorage: {{database_url: 'sqlite:///{workdir / 'e2e.db'}'}}\n"
    f"topology: {{parents: {{OUT_002: OUT_001}}}}\n"
    # Escalade après 3 h simulées (~9 s réelles) vers un canal webhook dont la destination n'écoute pas : l'alerte `escalated`
    # est enregistrée même si l'envoi échoue, et c'est elle que les tests lisent.
    f"notifications: {{enabled: true, escalate_after: 3h, channels: [{{name: astreinte, type: webhook, events: [escalated], secret_env: null}}]}}\n",
    encoding="utf-8"
)
os.environ.update(NM_CONFIG=str(workdir / "config.yaml"), NM_STATIC_DIR=str(frontend / "dist-e2e"), NM_AUTH_SECRET="e2e-secret", NM_WEBHOOK_URL="http://127.0.0.1:9/hook")

import uvicorn  # noqa: E402

from network_monitor.app import api, auth  # noqa: E402
from network_monitor.app.models import Severity  # noqa: E402
from network_monitor.data.mock import AnomalyScenario, ScenarioType  # noqa: E402

_hash = auth.hash_password("e2e-pass")
# alice : ancien format (sans rôle), donc administratrice : vérifie qu'un déploiement existant garde tous ses droits.
os.environ["NM_USERS"] = json.dumps({"alice": _hash, "bob": {"hash": _hash, "role": "operator"}, "carol": {"hash": _hash, "role": "viewer"}})

state = api.state
state.seconds_per_tick = 0.25
step = state.source.get_interval()
state.source.add_scenario(  # chute de trafic de 3 h 50 simulées, qui démarre 40 minutes simulées après le lancement
    AnomalyScenario(
        scenario_id="E2E", output_id="OUT_001", start_time=state.now + 8 * step, end_time=state.now + 54 * step,
        type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL, traffic_drop_fraction=0.7,
    )
)
state.source.add_scenario(  # même chute sur l'aval, un pas plus tard : rattachée à la cause amont, donc retenue (jamais notifiée)
    AnomalyScenario(
        scenario_id="E2E_DOWNSTREAM", output_id="OUT_002", start_time=state.now + 9 * step, end_time=state.now + 54 * step,
        type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL, traffic_drop_fraction=0.7,
    )
)
state.source.add_scenario(  # incident TARDIF (≈ 75 s après le lancement) et long : encore ouvert quand le test de prise en charge s'exécute
    AnomalyScenario(
        scenario_id="E2E_LATE", output_id="OUT_003", start_time=state.now + 300 * step, end_time=state.now + 600 * step,
        type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL, traffic_drop_fraction=0.7,
    )
)
uvicorn.run(api.app, host="127.0.0.1", port=port, log_level="warning")
