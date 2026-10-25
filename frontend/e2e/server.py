"""
Serveur de test pour Playwright : le **vrai** backend (boucle, détection, base SQLite, API), servant
l'interface compilée sur la même origine — exactement le montage de l'image Docker. Seules différences
avec la démonstration : deux incidents déterministes sont injectés (OUT_002 est déclarée en aval de OUT_001), la boucle va 8× plus vite que la
démonstration (0,25 s par pas de 5 min) et un utilisateur de test existe (alice / e2e-pass).

    python frontend/e2e/server.py <port> <dossier-temporaire>
"""

import hmac
import json
import os
import sys
from pathlib import Path

from fastapi import Header, HTTPException
from starlette.routing import Mount

port, workdir = int(sys.argv[1]), Path(sys.argv[2])
workdir.mkdir(parents=True, exist_ok=True)
frontend = Path(__file__).resolve().parent.parent

# L'environnement se règle AVANT l'import de l'API : le montage de l'interface et la configuration sont lus à l'import.
(workdir / "config.yaml").write_text(
    f"history: {{minimum: 30d, preferred: 40d}}\nstorage: {{database_url: 'sqlite:///{workdir / 'e2e.db'}'}}\n"
    f"topology: {{parents: {{OUT_002: OUT_001}}}}\n"
    # Escalade après 3 h simulées (~9 s réelles). Le webhook local absorbe les
    # notifications pour ne pas bloquer la boucle sur des retries réseau.
    f"notifications: {{enabled: true, escalate_after: 3h, channels: [{{name: astreinte, type: webhook, events: [escalated], secret_env: null}}]}}\n",
    encoding="utf-8"
)
os.environ.update(
    NM_CONFIG=str(workdir / "config.yaml"),
    NM_STATIC_DIR=str(frontend / "dist-e2e"),
    NM_AUTH_SECRET="e2e-secret",
    NM_WEBHOOK_URL=f"http://127.0.0.1:{port}/__e2e/webhook",
)

import uvicorn

from network_monitor.app import api, auth
from network_monitor.app.models import Severity
from network_monitor.data.mock import AnomalyScenario, ScenarioType

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
# L'incident OUT_003 est injecté à la demande par le test qui le prend en
# charge. Un horaire relatif au démarrage rendait la suite dépendante de sa
# durée réelle (notamment sous Windows, où Chromium est plus lent).
_E2E_CONTROL_TOKEN = "playwright-local-only"
_frontend_mount = next(
    (route for route in api.app.router.routes if isinstance(route, Mount) and route.name == "frontend"),
    None,
)
if _frontend_mount is not None:
    # The catch-all static mount was registered during API import. Temporarily
    # move it aside so these test-only API routes take precedence over it.
    api.app.router.routes.remove(_frontend_mount)


@api.app.post("/__e2e/webhook")
def _receive_e2e_webhook():
    """Puits local déterministe : les E2E testent l'événement, pas les retries réseau."""
    return {"ok": True}


@api.app.post("/__e2e/start-late-incident")
def _start_late_incident(x_e2e_control: str = Header(default="")):
    if not hmac.compare_digest(x_e2e_control, _E2E_CONTROL_TOKEN):
        raise HTTPException(status_code=403, detail="forbidden")
    start_time = state.now + 2 * step
    state.source.add_scenario(
        AnomalyScenario(
            scenario_id="E2E_LATE", output_id="OUT_003", start_time=start_time, end_time=start_time + 2500 * step,
            type=ScenarioType.TRAFFIC_DROP, severity=Severity.CRITICAL, traffic_drop_fraction=0.7,
        )
    )
    return {"start_time": start_time.isoformat()}


if _frontend_mount is not None:
    api.app.router.routes.append(_frontend_mount)


uvicorn.run(api.app, host="127.0.0.1", port=port, log_level="warning")
