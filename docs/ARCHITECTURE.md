# Architecture — Network Monitor

Ce document décrit le système **tel qu'il est dans le code** (commit courant). Les spécifications
d'origine sont dans `CAHIER_DES_CHARGES_RESEAU.md` ; ce qui n'est pas fait ou pas vérifié est dans
`LIMITES_ET_FEUILLE_DE_ROUTE.md` ; les mesures et le détail des corrections dans `../REVUE.md`.

## 1. Vue d'ensemble

Un seul processus Python (API + boucle de monitoring + envoi des alertes), un navigateur, et des
services externes optionnels.

```
                    ┌────────────────────────────── processus uvicorn (1 worker) ───────────────────────────────┐
 Navigateur         │                                                                                            │
 (React, Vite)      │  FastAPI ─ ETag → gzip → CORS            thread « monitoring »        thread « notifications »│
     │  HTTP/JSON   │  app/api.py                              MonitoringLoop (app/main.py)  NotificationDispatcher │
     └─────────────▶│  lecture : /outputs /history /incidents  ┌─ DataSource ──────────────┐ (app/notify.py)        │
                    │  /alerts /reports/availability           │ Mock · CSV · Parquet      │      │                 │
                    │  contrôle (connecté) : /config           │ SQL · Prometheus          │      │ HTTPS           │
                    │  /monitoring/* /notifications/*          └────────────┬──────────────┘      ▼                 │
                    │                                                       │ tick                Telegram (groupe)   │
                    │      ┌────────────────────── SqlAlchemyRepository ────▼───────────┐         SMS de groupe      │
                    │      │ SQLite (WAL) : raw_measurements · forecasts · anomalies ·  │◀────────  webhook signé       │
                    │      │ incidents · alerts · notifications (file d'envoi)          │ lit                         │
                    │      └───────────────────────────────┬────────────────────────────┘                            │
                    └──────────────────────────────────────┼─────────────────────────────────────────────────────────┘
                                                           ▼ à la purge
                                          archive Parquet (un dossier par sortie, un fichier par jour)
```

Le frontend ne contient **aucune logique de détection ni de génération de données** : `src/api.ts` est
un client HTTP typé, `Dashboard.tsx` affiche ce que l'API renvoie.

**Pourquoi un seul processus.** La boucle de monitoring vit dans le processus de l'API. Deux
processus sur la même base lanceraient deux boucles : alertes doublées, incidents dédoublés.
`app/lock.py` pose un verrou de fichier du système au démarrage (relâché par le noyau même après un
`kill -9`) et refuse de démarrer si un autre processus le tient ; `WEB_CONCURRENCY > 1` est refusé aussi,
et `uvicorn --workers 2` s'arrête (le second worker échoue, le parent s'arrête).

## 2. Ce qui se passe à chaque tick

`MonitoringLoop.run_tick(now)` (`app/main.py`), pour toutes les sorties :

1. **Lire** les nouvelles mesures (`DataSource.get_latest()`).
2. **Valider** (`DataValidator`) : drapeaux de qualité §31 (doublon, trafic négatif, pertes hors bornes, sortie
   inconnue, arrivée tardive) ; les créneaux attendus mais absents deviennent des trous explicites
   (`missing_slots`, idempotent : un trou n'est signalé qu'une fois).
3. **Prévoir** un pas (`ForecastModel.predict`) : baseline saisonnière, ou Holt-Winters si l'historique
   dépasse `model_selection.minimum_history` (45 jours) — choisi par rétro-test à la calibration.
4. **Détecter** (`analysis/detection.py`) : score de trafic (écart à l'attendu, normalisé), score de pertes
   (seulement une **hausse** compte), score conjoint, persistance.
5. **Enrichir** (sans effet sur la décision) : features §9 (`analysis/features.py`, stockées avec l'anomalie)
   et, si activé, score de l'Isolation Forest (`analysis/detection.py`).
6. **Décider** (`incidents/manager.py`, voir §4) : un `AnomalyResult` entre, des `Alert` et l'incident
   concerné sortent.
7. **Planifier les envois** : `NotificationDispatcher.plan()` transforme les alertes confirmées en lignes
   de la file d'envoi (une par alerte et par canal).
8. **Persister en UNE transaction** (`Repository.save_tick`) : mesures, prévisions, anomalies, incidents
   confirmés, alertes, envois. Une alerte et son envoi ne se séparent jamais ; un incident jamais
   confirmé n'est pas écrit.
9. **Recalibrer** si l'échéance est passée (§5).

Un échec sur un tick est journalisé et n'arrête jamais la boucle (`MonitoringState._run`).

## 3. Les modules

| Module | Rôle | À retenir |
|---|---|---|
| `app/models.py` | Contrats de données (Pydantic) | Horodatages toujours avec fuseau (UTC) ; `Incident` porte `started_at`/`ended_at` rétro-datés |
| `app/config.py` | Configuration typée, YAML en `safe_load` | Aucun champ sans effet ; seuils validés (`close < warning < open`) ; **aucun secret** : les canaux nomment des variables d'environnement, l'URL d'une source SQL ou Prometheus est un `SecretStr` jamais renvoyé par `GET /config` |
| `app/main.py` | Boucle de monitoring | Calibre **une sortie à la fois** (la mémoire ne dépend pas du nombre de sorties) ; une sortie non calibrée est réessayée chaque heure |
| `app/api.py` | API FastAPI | Voir §7 pour les points d'accès ; `PUT /config` revalide l'`AppConfig` entier avant d'appliquer |
| `app/auth.py` | Mots de passe scrypt, sessions signées HMAC à expiration, limiteur d'essais | `python -m network_monitor.app.auth hash` |
| `app/notify.py` | Alertes sortantes | Voir §6 |
| `app/http_cache.py` | ETag + `Cache-Control: no-cache` | 304 sans corps ; l'ETag se calcule sur le corps non compressé, avant le gzip |
| `app/lock.py` | Verrou d'instance unique | Voir §1 |
| `data/source.py` | Contrat `DataSource` | `load_history(start, end, output_id=None)` : début inclus, fin exclue ; `get_latest()` ne rend jamais deux fois la même mesure |
| `data/mock.py` | Source simulée (3 sorties) | Bruit tiré d'un générateur **à état** : deux lectures d'une même plage ne rendent pas les mêmes valeurs |
| `data/sources.py` | CSV, Parquet (Polars), SQL (SQLAlchemy), Prometheus (`query_range`) | Données en colonnes, objets Python seulement pour la fenêtre demandée ; SQL en lecture seule avec identifiants validés ; Prometheus : curseur qui n'avance qu'après une réponse réussie |
| `analysis/forecasting.py` | Baseline saisonnière, Holt-Winters, sélection | `predict` suit l'horizon demandé (décalage depuis la fin d'ajustement) |
| `analysis/detection.py` | Scores, baseline de pertes, Isolation Forest optionnel | La forêt n'ouvre jamais d'incident : elle enrichit les preuves |
| `analysis/availability.py` | Rapport de disponibilité | Union des intervalles, dégradation, couverture, MTTD, MTTR ; calculé sur la **période observée** (depuis la première mesure conservée), pas sur la période demandée |
| `analysis/evaluation.py` | Banc d'évaluation §33 | `capture` → `replay` → `sweep` : les anomalies sont enregistrées une fois puis rejouées avec d'autres seuils |
| `incidents/manager.py` | Machine à états des incidents | Voir §4 |
| `storage/repository.py` | SQLite/SQLAlchemy (tables `Mapped[...]`) | WAL, un commit par tick, purge avec archivage |
| `storage/archive.py` | Archive Parquet | Écriture atomique et idempotente ; relisible par `ParquetDataSource` |

## 4. Le moteur d'incidents

États : `SUSPECT` → `OPEN` → `CLOSED` (ou `STALE` si les mesures s'arrêtent). Réglages par défaut
choisis par mesure (`REVUE.md`) : confirmation après 15 min au-dessus de `open_score`, fermeture après
5 min sous `close_score`, `min_suspect_steps = 3`.

- **Signal faible ≠ incident.** Un `SUSPECT` qui retombe sans confirmation n'est jamais écrit en base
  (`incident_worth_persisting`) et ne notifie personne. Un pic isolé n'a droit à aucune tolérance ; au-delà
  de `min_suspect_steps` pas actifs, un creux ne referme plus le `SUSPECT` (un incident intermittent reste un seul incident).
- **Dates rétro-datées.** `started_at` = premier signal faible de l'épisode ; `ended_at` = premier pas
  sous `close_score`. `opened_at` et `closed_at` restent les dates de *décision*. La durée d'un incident,
  les alertes de fermeture et le rapport de disponibilité utilisent les dates rétro-datées.
- **Fusion.** Un nouvel épisode **confirmé** qui commence moins de `merge_window` (20 min) après la fin d'un
  incident rouvre ce même incident (même identité, début d'origine conservé). Un pic de bruit juste après une fin ne le prolonge pas.
- **Type.** Instantané (chute, pic, pertes, congestion) puis requalifié par le motif de l'écart : alternance
  avec hystérésis → intermittent, montée corrélée → progressif. Figé pendant le retour à la normale.
- **Preuves plafonnées** à 200 entrées (début et fin conservés).

## 5. Temps, calibration, recalibration

- **Deux horloges.** Source simulée (Mock) : l'horloge avance de `interval` toutes les `seconds_per_tick`
  (2 s) et part de l'heure actuelle arrondie à la grille. Rejeu d'un fichier (CSV, Parquet) : elle part de
  la bascule historique → rejeu. Source « live » (SQL, Prometheus) : horloge réelle, un tick par `interval`.
- **Le serveur est la seule source du « maintenant ».** `GET /monitoring/status` donne `simulated_now` et
  `server_time` ; les fenêtres se demandent en `span_hours` (ou `since` = dernier point connu), les rapports
  en `days`. Aucun client ne fabrique d'instants à partir de sa propre horloge.
- **Calibration** (`calibrate`) : pour chaque sortie, charge `history.preferred` (40 jours par défaut en
  démonstration, 90 sinon), choisit le modèle, apprend la baseline de pertes et, si activé, la forêt. Sous
  `history.minimum`, la sortie est ignorée (journalisé) et réessayée chaque heure.
- **Recalibration** (§32) : toutes les 24 h, hors du chemin critique, en excluant les plages couvertes par un
  incident actif (pour ne pas apprendre l'anomalie comme un comportement normal), puis purge des mesures
  plus vieilles que `history.preferred` — archivées en Parquet si `storage.archive_dir` est défini.

## 6. Alertes sortantes (`app/notify.py`)

File d'envoi transactionnelle (« outbox ») : l'alerte et ses lignes d'envoi sont écrites dans la **même transaction**.
Un thread relit les envois dus toutes les 2 s :

- **Regroupement** : les alertes d'un canal arrivées à moins de `batch_window` partent dans un seul message.
- **Plafonds** par minute/heure : au-delà, les alertes s'accumulent et partent regroupées quand la fenêtre se libère.
- **Nouvelles tentatives** à délai croissant (plafonné à 15 min, `retry_after` de Telegram respecté) ; un refus
  définitif (jeton invalide) n'est pas retenté ; pour un SMS de groupe, seuls les numéros en échec sont retentés.
- **Canaux** : Telegram (groupe), webhook (JSON signé HMAC-SHA256, identifiant de livraison stable pour dédupliquer),
  SMS (passerelle Android en un appel pour tout le groupe, ou API Orange un appel par numéro, jeton OAuth mis en cache).
  Un SMS tient en un segment de 160 caractères sans accents.
- **Garantie** : au moins une fois. **Un secret absent refuse le démarrage** (mieux vaut échouer au lancement que
  découvrir pendant un incident qu'aucune alerte ne part). Un jeton ne figure jamais dans les erreurs enregistrées.

## 7. API

Lecture, sans connexion : `GET /health`, `/outputs`, `/outputs/{id}/history|anomalies|forecast|forecast/ahead`,
`/incidents`, `/incidents/{id}`, `/alerts` (par défaut sans les signaux faibles ; `all=true` pour l'audit), `/reports/availability` (JSON ou CSV), `/monitoring/status`, `/config`.
Avec connexion (`Authorization: Bearer`, session obtenue par `POST /auth/login` ou jeton machine `NM_API_TOKEN`) :
`PUT /config`, `POST /monitoring/start|stop`, `GET /notifications`, `POST /notifications/test/{canal}`.

Garde-fous : datetimes sans fuseau refusés (422), fenêtres limitées à 31 jours (rapports : 366), erreurs de
validation de `PUT /config` structurées (champ et message, sans la valeur saisie ni l'URL de documentation), `/docs` et `/openapi.json` masqués hors `NM_ENV=development`, CORS limité à
`NM_CORS_ORIGINS`, sans authentification configurée les endpoints de contrôle répondent 503 (ils ne restent
jamais ouverts par défaut).

**Trafic.** gzip au-delà de 500 octets, ETag (304 sans corps), et côté client : rafraîchissement incrémental des
graphes, polling suspendu quand l'onglet est masqué. Mesuré : 66,0 → 1,7 Ko par cycle de rafraîchissement.

## 8. Stockage

SQLite en mode WAL (les lectures de l'API ne sont pas bloquées par les écritures de la boucle), un commit par
tick. Tables : `raw_measurements`, `forecasts`, `anomalies` (scores, écarts, features, score de forêt),
`incidents` (preuves en JSON), `alerts`, `notifications`. **Pas de migration** : le schéma est créé au
démarrage ; après une évolution du modèle de données, supprimer la base (rien à conserver à ce stade). Les
mesures purgées vont dans `archive_dir/<sortie encodée>/<jour>.parquet`.

## 9. Frontend (`frontend/src`)

| Fichier | Rôle |
|---|---|
| `main.tsx` | Point d'entrée ; polices **auto-hébergées** (`@fontsource`, sous-ensemble latin) : aucune requête vers un tiers, l'apparence est la même sans accès à Internet |
| `api.ts`, `types.ts` | Client HTTP typé ; `VITE_API_BASE_URL` (chaîne vide = même origine, cas de l'image Docker) ; session en mémoire (jamais dans le stockage du navigateur) |
| `chart.ts` | Fonctions pures : décimation min/max (les pics ne sont jamais lissés), point le plus proche, pointeur → temps, fusion incrémentale |
| `polling.ts` | `usePolling` : appelle tout de suite puis à chaque période, **sauf onglet masqué** |
| `Dashboard.tsx` | Interface « Signal Room » : sorties, graphe détaillé (souris et clavier), tableau comparatif, rapport de disponibilité, alertes, incidents, configuration et connexion |

Cadence : sorties/incidents/état toutes les 4 s, graphe déplié toutes les 5 s (incrémental : seulement les
points postérieurs au dernier connu, invalidation des réponses en vol quand la fenêtre change), tableau comparatif
toutes les 15 s, rapport de disponibilité toutes les 60 s. Le tableau comparatif reste coûteux : 8 requêtes par sortie et
par rafraîchissement (4 fenêtres × valeur courante et précédente), faute d'endpoint « valeur la plus proche d'un instant ».

Accessibilité (vérifiée par axe-core dans un vrai navigateur, WCAG 2.x A/AA) : repères (`header`, `main`, `section`),
lien d'évitement, zones `aria-live` pour alertes et incidents, graphe focalisable avec flèches/Origine/Fin/Échap, un seul
contrôle clavier par en-tête repliable (le bouton ; le clic sur l'en-tête n'est qu'un confort à la souris, sans rôle « bouton »
imbriqué), contrastes de texte ≥ 4,5:1.

## 10. Déploiement en conteneur

`Dockerfile` en deux étapes : build du frontend (`VITE_API_BASE_URL=""`), puis image Python avec les seules
dépendances d'exécution (`requirements.txt`) et l'interface compilée servie par l'API (`NM_STATIC_DIR`) :
une origine, un processus. Utilisateur sans privilèges, `/data` en volume (base, verrou, archive), un seul
worker. `docker-compose.yml` : écoute sur `127.0.0.1` uniquement, système de fichiers en lecture seule, aucune
capacité Linux. Le TLS et l'exposition publique ne sont pas inclus. **L'image n'a pas été construite dans
l'environnement de développement** (pas de Docker) : voir `LIMITES_ET_FEUILLE_DE_ROUTE.md`.

## 11. Contrainte assumée : pas d'historique avant le démarrage

`GET /outputs/{id}/history` ne rend que ce que la boucle a réellement traité et stocké depuis son démarrage ;
l'historique de calibration n'est pas persisté. Avec le Mock accéléré (2 s réelles par pas de 5 min simulées) :
12 h de courbe demandent environ 5,5 minutes de fonctionnement, 1 jour 11,5 minutes, 1 semaine 1,1 heure,
1 mois 4,8 heures. Avec une source live (SQL, Prometheus), c'est du temps réel : la plage affichable croît
d'autant. Le message explicite du graphe (« pas encore assez d'historique ») est un comportement correct, pas un défaut à masquer.

## 12. Vérification

- `pytest` (backend), `ruff`, `mypy` : voir le README pour les commandes ; des tests tournent contre de vrais
  services quand `NM_TEST_MARIADB_URL` et `NM_TEST_PROMETHEUS_URL` sont définis (MariaDB 10.11, Prometheus 2.45).
- `npm run typecheck`, `npm test` (Vitest, jsdom) pour le frontend.
- `npm run e2e` (Playwright, Chromium) : le vrai backend (`frontend/e2e/server.py`, incident injecté, boucle 8× plus rapide)
  servant l'interface compilée sur la même origine, comme l'image Docker. Couvre ce que jsdom ne peut pas : géométrie
  réelle (infobulle à la souris, libellés d'axe non tronqués, pas de débordement à 375 px), gzip/304/incrémental vus par
  le navigateur, suspension du polling onglet masqué, connexion, rapport de disponibilité et téléchargement CSV, audit axe.
  Chaque test échoue si on remet le défaut qu'il garde (vérifié pour la marge d'axe et le libellé « maintenant »).
- Mesures et protocoles : `../REVUE.md` (balayage des seuils sur 96 scénarios, trafic du dashboard, mémoire à
  30 et 90 sorties, lecture de 1 M de lignes). Elles viennent du Mock : à refaire sur des données réelles.
