# Network Monitor — projet complet (backend + frontend)

Système de détection d'anomalies réseau non supervisée : backend Python
(FastAPI) + dashboard React branché dessus par de vrais appels réseau.
Aucune simulation côté client — toutes les données affichées viennent du
backend, qui les génère (source Mock), les analyse et les stocke.

## État du projet

Projet de démonstration (source de données Mock uniquement). La revue
complète et le suivi des correctifs sont dans **[`REVUE.md`](REVUE.md)**.

Limites connues :

- Sources : Mock (défaut), rejeu d'un export CSV et table SQL en lecture seule (`source.type`, voir `config.example.yaml`). Le contrat commun est vérifié sur le Mock, un CSV, SQLite et **MariaDB 10.11** (`NM_TEST_MARIADB_URL`, voir plus bas), y compris la boucle de monitoring de bout en bout et la reprise après une connexion coupée. Aucune n'a été éprouvée sur un vrai réseau.
- Authentification : identifiant et mot de passe (`NM_USERS`, mots de passe hachés scrypt) donnent un jeton de session signé à durée limitée, avec limitation des essais. Ce n'est ni un annuaire (pas de SSO/LDAP) ni une gestion de rôles : tout utilisateur connecté peut tout modifier.
- Les seuils d'incident ont été réglés par mesure sur le Mock seulement (8 jeux, 96 scénarios) : 0,04 faux incident par sortie et par jour, 87 scénarios détectés sur 96 (les 9 manqués sont des intermittents dont un seul échantillon sur trois tombe dans la phase active). Voir `REVUE.md`.
- Le schéma SQLite est créé au démarrage sans migration : après une évolution du modèle de données, supprimer `network_monitor.db` (aucune donnée à conserver à ce stade).
- L'Isolation Forest (`detection.isolation_forest.enabled`, désactivé par défaut, nécessite `pip install scikit-learn`) enrichit les preuves d'un incident sans jamais en ouvrir un.

## Structure

```
network_monitor/        Backend Python (le paquet importable)
  app/
    models.py           Contrats de données (Pydantic)
    config.py           Configuration typée (YAML, sécurisé)
    auth.py             Mots de passe (scrypt), sessions signées, limitation d'essais
    main.py             Boucle de monitoring (§30 du cahier des charges réseau)
    api.py              API FastAPI — la porte d'entrée du frontend
  data/                  DataSource : Mock, rejeu CSV, table SQL (sources.py)
  analysis/              Prévision, détection, features (§9), Isolation Forest (§17), évaluation (§33)
  incidents/             Moteur d'incidents (machine à états, hystérésis)
  storage/               Persistance SQLite/SQLAlchemy
  tests/                 tests (unitaires, scénarios, API, sources, régressions)

frontend/                Dashboard React + TypeScript strict (Vite), branché sur l'API
  src/
    api.ts, types.ts     Client HTTP typé vers le backend — aucune génération de données
    chart.ts             Décimation et recherche du point le plus proche (fonctions pures)
    Dashboard.tsx        Interface "Signal Room"
    *.test.ts(x)         Tests Vitest + Testing Library
  .env.example            VITE_API_BASE_URL à copier en .env

docs/
  CAHIER_DES_CHARGES_RESEAU.md   Spécifications du système de détection
  ARCHITECTURE.md                 Documentation technique du projet complet

config.example.yaml      Exemple de configuration (source, seuils, Isolation Forest)
requirements.txt         Dépendances Python (scikit-learn : optionnel, Isolation Forest)
```

## Démarrage rapide

**1. Backend :**

```bash
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
python -m network_monitor.app.auth hash        # génère le hachage d'un mot de passe
export NM_USERS='{"alice": "<hachage>"}'        # utilisateurs autorisés (Windows : set NM_USERS=...)
export NM_AUTH_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")  # clé de signature des sessions
# jeton machine optionnel pour des scripts : export NM_API_TOKEN=change-me
python -m uvicorn network_monitor.app.api:app --reload
```

L'API calibre automatiquement 3 sorties Mock et lance le monitoring au
démarrage (compter quelques secondes). Documentation interactive sur
`http://127.0.0.1:8000/docs` (masquée si `NM_ENV` ≠ `development`).

Variables d'environnement : `NM_USERS`, `NM_AUTH_SECRET`, `NM_TOKEN_TTL_MINUTES` (60 par défaut), `NM_API_TOKEN` (jeton machine des endpoints de
contrôle ; sans lui ils répondent 503), `NM_CORS_ORIGINS` (origines
autorisées, `http://localhost:5173` par défaut), `NM_ENV`, `NM_CONFIG` (chemin d'un
YAML, voir `config.example.yaml`, pour choisir la source de données et les seuils).

**2. Frontend**, dans un second terminal :

```bash
cd frontend
cp .env.example .env   # (Windows : copy .env.example .env) ; ajuster VITE_API_BASE_URL si besoin
npm install
npm run dev            # la connexion se fait dans le panneau Configuration
```

Ouvrir `http://localhost:5173`.

**3. Tests :**

```bash
python -m pytest network_monitor/tests/ -v      # backend (NM_TEST_MARIADB_URL=mysql+pymysql://user:pass@host/db active aussi les tests MariaDB)
cd frontend && npm run typecheck && npm test    # frontend (tsc + Vitest)
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

Le dashboard demande ses fenêtres à l'API en heures relatives à l'horloge
**du serveur** (`span_hours`) : l'horloge simulée n'est pas celle du navigateur.

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
