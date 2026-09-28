# Network Monitor — projet complet (backend + frontend)

Système de détection d'anomalies réseau non supervisée : backend Python
(FastAPI) + dashboard React branché dessus par de vrais appels réseau.
Aucune simulation côté client — toutes les données affichées viennent du
backend, qui les génère (source Mock), les analyse et les stocke.

## ⚠️ État du projet

Ce dépôt est publié à l'état de développement, **sans garantie de fonctionnement**.
Une revue complète a été réalisée : voir **[`REVUE.md`](REVUE.md)**.

Points bloquants identifiés (détails, correctifs et priorisation dans `REVUE.md`) :

- `npm install` / `npm ci` **échouent** (`@vitejs/plugin-react` incompatible avec `vite@8`) — le frontend n'est pas installable à froid. Contournement : `npm install --legacy-peer-deps`.
- La suite de tests n'est **pas verte** : sur 66 tests, 54 passent, 12 échouent et 5 erreurient (descripteur SQLite non libéré au teardown).
- L'API n'a **aucune authentification** et autorise CORS sur toutes les origines : ne l'exposez pas sur un réseau autre que `localhost`.
- Un tick de monitoring prend ~1,5 s, à cause de `fsync` SQLite (corrigeable par `PRAGMA journal_mode=WAL` + `synchronous=NORMAL`, gain mesuré 23×).

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
  tests/                 66 tests (unitaires, scénarios, API)

frontend/                Dashboard React (Vite), branché sur l'API
  src/
    api.js               Client HTTP vers le backend — aucune génération de données
    Dashboard.jsx         Interface "Signal Room"
  .env.example            VITE_API_BASE_URL à copier en .env

docs/
  CAHIER_DES_CHARGES_RESEAU.md   Spécifications du système de détection
  ARCHITECTURE.md                 Documentation technique du projet complet

requirements.txt         Dépendances Python
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

**3. Tests backend :**

```bash
python -m pytest network_monitor/tests/ -v
```

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

## Historique du projet

Ce projet a été construit en plusieurs étapes : d'abord le backend seul
(cahier des charges réseau, `docs/CAHIER_DES_CHARGES_RESEAU.md`), puis des
maquettes de dashboard purement simulées côté client (utiles pour valider
le design avant de coder l'intégration réelle), puis l'API FastAPI, et
enfin ce frontend branché dessus. Les maquettes simulées ne sont pas
incluses ici : ce projet est la version connectée, destinée à être
réellement utilisée.
