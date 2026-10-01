# Network Monitor — projet complet (backend + frontend)

Système de détection d'anomalies réseau non supervisée : backend Python
(FastAPI) + dashboard React branché dessus par de vrais appels réseau.
Aucune simulation côté client — toutes les données affichées viennent du
backend, qui les génère (source Mock), les analyse et les stocke.

## État du projet

Revue initiale : [`REVUE.md`](REVUE.md). Corrections, mesures et rectificatifs : [`PLAN_CORRECTION.md`](PLAN_CORRECTION.md).

**Limites connues :**

- L'API n'a aucune authentification : ne l'exposez pas au-delà de `localhost`.
- Le frontend est testé sous jsdom et par son build, pas dans un navigateur réel (rendu, responsive et
  lecteurs d'écran à contrôler à la main).
- Les seuils d'incident par défaut sont des valeurs initiales. Sur 6 graines du Mock : rappel 0,93 et
  environ 2 faux positifs par 2 jours (voir « Évaluation quantitative »). À régler sur vos données.
- Migration de schéma : seuls les ajouts de colonnes nullables sont automatiques.
- Sur le Mock, la baseline saisonnière est toujours retenue face à Holt-Winters.

## Structure

```
network_monitor/        Backend Python (le paquet importable)
  app/
    models.py           Contrats de données (Pydantic)
    config.py           Configuration typée (YAML, sécurisé)
    main.py             Boucle de monitoring (§30 du cahier des charges réseau)
    api.py              API FastAPI — la porte d'entrée du frontend
  data/                  DataSource + Mock (générateur de données)
  analysis/              Forecasting + détection d'anomalies
  incidents/             Moteur d'incidents (machine à états, hystérésis)
  storage/               Persistance SQLite/SQLAlchemy
  evaluation/            Évaluation quantitative (§33) : métriques, banc, CLI
  tests/                 161 tests (unitaires, scénarios, API, maintenance, évaluation, non-régression)

frontend/                Dashboard React (Vite), branché sur l'API
  src/
    api.js               Client HTTP (délai maximal, annulation) — aucune génération de données
    lib.js               Fonctions pures (sous-échantillonnage min/max, formats, recul)
    Dashboard.jsx         Interface "Signal Room"
    ErrorBoundary.jsx     Évite l'écran blanc en cas d'exception de rendu
    *.test.js(x)          37 tests (vitest + jsdom + Testing Library)
  .env.example            VITE_API_BASE_URL à copier en .env

docs/
  CAHIER_DES_CHARGES_RESEAU.md   Spécifications du système de détection
  ARCHITECTURE.md                 Documentation technique du projet complet

requirements.txt         Dépendances Python (pyproject.toml : idem + outils de développement)
.github/workflows/ci.yml  CI : ruff, mypy, pytest (Python 3.11/3.12), tests + build du frontend
```

## Démarrage rapide

**1. Backend :**

```bash
pip install -r requirements.txt --break-system-packages
python -m uvicorn network_monitor.app.api:app --reload
```

L'API calibre automatiquement 3 sorties Mock et lance le monitoring au
démarrage (compter quelques secondes). Documentation interactive sur
`http://127.0.0.1:8000/docs`.

**2. Frontend**, dans un second terminal :

```bash
cd frontend
cp .env.example .env   # ajuster VITE_API_BASE_URL si besoin
npm install
npm run dev
```

Ouvrir `http://localhost:5173`.

**3. Tests, lint, typage :**

```bash
pip install -e ".[dev]"
pytest -q                          # 161 tests backend
ruff check network_monitor && mypy network_monitor
cd frontend && npm test            # 37 tests frontend
```

**4. Évaluation quantitative (§33)** — détection et prévision comparées à la vérité terrain du Mock,
sur une période distincte de la calibration :

```bash
python -m network_monitor.evaluation            # rapport lisible (≈ 5 s)
python -m network_monitor.evaluation --seed 7 --days 2 --json --out rapport.json
```

Sur 6 graines (défauts actuels) : rappel 0,93, précision 0,82, ~2,2 faux positifs / 2 jours, délai de
détection moyen ≈ 10 min ; le scénario `intermittent_incident` est le point faible. Passer
`minimum_duration` à 15 min supprime presque tous les faux positifs (précision 0,98) mais fait manquer
les incidents courts (15 min). C'est un compromis à trancher, pas un défaut : voir `PLAN_CORRECTION.md`.

**Configuration** : origines CORS autorisées via `NETWORK_MONITOR_CORS_ORIGINS` (liste séparée par des
virgules, défaut : `http://localhost:5173,http://127.0.0.1:5173`). Recalibration
(`monitoring.recalibration_interval`, défaut 24 h, `null` pour désactiver) et rétention
(`storage.retention`, défaut 30 j, `null` pour illimitée) : voir `network_monitor/app/config.py`.

## Ce que fait le dashboard

Reprend l'identité visuelle "Signal Room" (console façon oscilloscope/radar)
des maquettes précédentes, mais entièrement branché sur l'API réelle :

- **Vue par sortie**, repliable : statut courant, graphe détaillé (double
  axe trafic/pertes, plages 12 h/1 j/1 sem/1 mois), courbes masquables
  (trafic, pertes, baseline, prédiction — ces deux dernières récupérées
  via `/outputs/{id}/forecast` et `/outputs/{id}/forecast/ahead`).
- **Tableau comparatif** : trafic (Gbps) et pertes (%), 4 fenêtres
  temporelles, variations colorées au-delà d'un seuil ajustable.
- **Panneau de configuration** : les seuils du moteur d'incidents sont
  envoyés à l'API via `PUT /config` et s'appliquent réellement à la
  détection en cours — pas un réglage cosmétique local.
- **Liste des incidents**, actifs et récents, lue directement depuis
  `GET /incidents`.

## Point d'attention important : le temps réel du Mock

Le backend n'a de données que depuis son démarrage — il ne peut pas
fabriquer un mois d'historique à la demande comme le ferait une pure
simulation cliente. Pour rester utilisable en démonstration, la source
Mock avance son horloge simulée à un rythme accéléré (2 secondes réelles
par pas de 5 minutes simulées, voir `MonitoringState.seconds_per_tick`
dans `app/api.py`) — mais une plage "1 mois" nécessite quand même environ
5 heures de fonctionnement réel du serveur avant d'avoir de quoi
l'afficher. Le dashboard gère ce cas proprement (message explicite plutôt
qu'un graphe vide ou cassé) plutôt que de le cacher. Voir
`docs/ARCHITECTURE.md` pour le détail.

**Conséquence pour tout client de l'API** : les horodatages sont dans l'horloge *simulée*, qui avance
150× plus vite que l'horloge réelle. Ne jamais calculer une fenêtre avec `new Date()` : lire
`simulated_now` (`GET /monitoring/status`), ou omettre `until` (le serveur prend alors sa propre
horloge). Le dashboard faisait l'erreur — après 40 s, 1 point sur 21 seulement était récupéré.

## Historique du projet

Ce projet a été construit en plusieurs étapes : d'abord le backend seul
(cahier des charges réseau, `docs/CAHIER_DES_CHARGES_RESEAU.md`), puis des
maquettes de dashboard purement simulées côté client (utiles pour valider
le design avant de coder l'intégration réelle), puis l'API FastAPI, et
enfin ce frontend branché dessus. Les maquettes simulées ne sont pas
incluses ici : ce projet est la version connectée, destinée à être
réellement utilisée.
