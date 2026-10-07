# Network Monitor — projet complet (backend + frontend)

Système de détection d'anomalies réseau non supervisée : backend Python
(FastAPI) + dashboard React branché dessus par de vrais appels réseau.
Aucune simulation côté client — toutes les données affichées viennent du
backend, qui les génère (source Mock), les analyse et les stocke.

## État du projet

Projet de démonstration (source de données Mock uniquement). La revue
complète et le suivi des correctifs sont dans **[`REVUE.md`](REVUE.md)**.

Limites connues (liste complète, avec ce qui est vérifié ou non : `docs/LIMITES_ET_FEUILLE_DE_ROUTE.md`) :

- Sources : Mock (défaut), rejeu CSV ou Parquet (Polars), table SQL en lecture seule (testée sur SQLite et MariaDB 10.11) et **Prometheus** (`query_range`, validée sur un vrai Prometheus avec node_exporter). Aucune n'a été éprouvée sur un vrai réseau ni sur des compteurs SNMP d'équipements.
- Alertes : Telegram (groupe), webhook signé et SMS de groupe sont testés contre des serveurs **simulés**, jamais contre les vrais services. Premier essai réel : `POST /notifications/test/{canal}`.
- Authentification : identifiant et mot de passe (`NM_USERS`, scrypt), session signée à durée limitée, limitation des essais. Ni annuaire, ni rôles : tout utilisateur connecté peut tout modifier.
- Seuils d'incident réglés par mesure sur le Mock seulement (96 scénarios) : 0,04 faux incident par sortie et par jour, 87 scénarios détectés sur 96 (les 9 manqués sont des intermittents épars). Voir `REVUE.md`.
- L'image Docker n'a pas pu être construite dans l'environnement de développement (pas de Docker) : ses étapes ont été reproduites à la main, voir `REVUE.md`.
- Le schéma SQLite est créé au démarrage sans migration : après une évolution du modèle de données, supprimer `network_monitor.db` (rien à conserver à ce stade).

## Structure

```
network_monitor/        Backend Python (le paquet importable)
  app/
    models.py           Contrats de données (Pydantic)
    config.py           Configuration typée (YAML, sécurisé)
    auth.py             Mots de passe (scrypt), sessions signées, limitation d'essais
    notify.py           Alertes sortantes : file d'envoi, Telegram, webhook, SMS
    http_cache.py       ETag / 304 (le gzip vient de Starlette)
    lock.py             Verrou d'instance unique (un seul worker)
    main.py             Boucle de monitoring (§30 du cahier des charges réseau)
    api.py              API FastAPI — la porte d'entrée du frontend
  data/                  DataSource : Mock, rejeu CSV, table SQL (sources.py)
  analysis/              Prévision, détection, features (§9), Isolation Forest (§17), évaluation (§33), disponibilité
  incidents/             Moteur d'incidents (machine à états, hystérésis)
  storage/               Persistance SQLite/SQLAlchemy, archive Parquet des mesures purgées
  tests/                 tests (unitaires, scénarios, API, sources, régressions)

frontend/                Dashboard React + TypeScript strict (Vite), branché sur l'API
  src/
    api.ts, types.ts     Client HTTP typé vers le backend — aucune génération de données
    chart.ts             Décimation et recherche du point le plus proche (fonctions pures)
    Dashboard.tsx        Interface "Signal Room"
    main.tsx             Point d'entrée, polices auto-hébergées (@fontsource)
    *.test.ts(x)         Tests Vitest + Testing Library
  e2e/                   Tests Playwright (navigateur réel) et serveur de test déterministe
  .env.example            VITE_API_BASE_URL à copier en .env

docs/
  CAHIER_DES_CHARGES_RESEAU.md   Spécifications du système de détection
  ARCHITECTURE.md                 Documentation technique du projet complet
  LIMITES_ET_FEUILLE_DE_ROUTE.md  Limites, fonctionnalités proposées, décisions reportées

config.example.yaml      Exemple de configuration (source, seuils, Isolation Forest)
requirements.txt         Dépendances d'exécution (optionnel : scikit-learn, pymysql)
requirements-dev.txt     + pytest, ruff, mypy
Dockerfile, docker-compose.yml, .env.example   Déploiement en conteneur
```

## Démarrage rapide

**1. Backend :**

```bash
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements-dev.txt
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
autorisées, `http://localhost:5173` par défaut), `NM_ENV`, `NM_STATIC_DIR` (sert l'interface compilée), `NM_CONFIG` (chemin d'un
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
python -m pytest network_monitor/tests/ -v      # backend ; NM_TEST_MARIADB_URL=mysql+pymysql://u:p@host/db et NM_TEST_PROMETHEUS_URL=http://host:9090 activent les tests contre de vrais services
cd frontend && npm run typecheck && npm test    # frontend (tsc + Vitest, jsdom)
cd frontend && npx playwright install chromium   # une fois
cd frontend && npm run e2e                        # navigateur réel + vrai backend (voir ci-dessous)
```

`npm run e2e` (Playwright) construit l'interface en mode « e2e » (même origine, comme l'image Docker), lance le **vrai** backend
avec un incident injecté (`frontend/e2e/server.py`) et pilote Chromium : infobulle à la souris, libellés d'axe, gzip/304/incrémental,
onglet masqué, connexion, rapport de disponibilité, téléchargement CSV, audit d'accessibilité axe-core. Dans un environnement qui ne
peut pas télécharger les navigateurs : `PW_CHROMIUM_PATH=/chemin/vers/chromium` (et `PW_NO_SANDBOX=1` sous root).

## Docker

```bash
cp .env.example .env && cp config.example.yaml config.yaml     # puis les adapter
docker compose up -d --build                                    # http://127.0.0.1:8000
```

Une seule image : l'API sert aussi l'interface (même origine, pas de CORS). Elle tourne sous un utilisateur sans privilèges, avec **un seul worker** (la boucle de monitoring est dans le processus : un second processus sur la même base est refusé au démarrage, et `uvicorn --workers 2` s'arrête). La base, son verrou et l'archive Parquet vivent dans le volume `/data`. Le TLS et l'exposition publique ne sont pas inclus : l'image écoute sur `127.0.0.1`.

## Alertes sortantes

Dans `config.yaml` (voir `config.example.yaml`) ; les secrets restent dans `.env`, la configuration ne nomme que les variables :

```yaml
notifications:
  enabled: true
  timezone: Africa/Douala
  channels:
    - {name: noc, type: telegram, chat_id: "-1001234567890"}                        # NM_TELEGRAM_TOKEN
    - {name: ops-sms, type: sms, provider: android_gateway,                          # NM_SMS_URL / _USER / _PASSWORD
       recipients: ["+237690000001", "+237690000002"], min_severity: critical}
    - {name: ticketing, type: webhook}                                               # NM_WEBHOOK_URL / _SECRET
```

- **Une alerte, un envoi par canal, écrit dans la même transaction** (file d'envoi) : un redémarrage ne perd rien. Un signal faible jamais confirmé ne notifie personne.
- **Regroupement** : les alertes rapprochées (`batch_window`) partent dans un seul message ; **plafonds** par minute/heure : au-delà, elles s'accumulent et partent regroupées, rien n'est perdu.
- **Nouvelles tentatives** à délai croissant (et `retry_after` de Telegram) ; un refus définitif n'est pas retenté ; pour un SMS de groupe, seuls les numéros en échec sont retentés.
- **SMS** : un seul segment (160 caractères, sans accents). `android_gateway` : un téléphone Android avec une SIM locale ; `orange` : API Orange Developer (forfait SMS à acheter). **Webhook** : JSON signé HMAC-SHA256 (`X-NM-Signature`, `X-NM-Timestamp`) avec un identifiant de livraison stable pour dédupliquer.
- Vérifier un canal : `POST /notifications/test/{canal}` (connecté) ; état des envois : `GET /notifications`.

## Rapport de disponibilité

`GET /reports/availability?days=30` (ou `start`/`end`), `&format=csv`, `&output_id=…`, et le panneau « Disponibilité » du dashboard. Par sortie : indisponibilité (incidents critiques, union des intervalles), dégradation, couverture des mesures, MTTD, MTTR — calculés sur les dates **rétro-datées** de l'incident (`started_at` : premier signal faible, `ended_at` : premier pas de retour à la normale), pas sur les dates de confirmation et de fermeture. La disponibilité est rapportée sur le temps écoulé ; une couverture basse la rend moins fiable.

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
