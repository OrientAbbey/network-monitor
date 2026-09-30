# Documentation technique — Network Monitor (backend + frontend)

## 1. Vue d'ensemble

Deux processus séparés qui communiquent en HTTP :

```
┌─────────────────────┐        HTTP (JSON)        ┌──────────────────────┐
│  Frontend (Vite/React) │ ───────────────────────▶ │  Backend (FastAPI)   │
│  localhost:5173         │ ◀─────────────────────── │  localhost:8000       │
└─────────────────────┘                            └──────────────────────┘
                                                       │
                                                       ├─ MockDataSource (génère les mesures)
                                                       ├─ MonitoringLoop (§30 : valide, prévoit,
                                                       │   détecte, gère les incidents, à chaque tick)
                                                       └─ SqlAlchemyRepository (SQLite)
```

Le frontend ne contient **aucune logique de génération de données ni de
détection** : `src/api.js` n'est qu'un client HTTP, `Dashboard.jsx` ne fait
qu'afficher ce que l'API renvoie. C'est la différence structurante avec les
maquettes précédentes du même projet, qui simulaient tout côté client.

## 2. Backend : ce qui a changé pour devenir une API

Le cœur (`data/`, `analysis/`, `incidents/`, `storage/`) est inchangé —
voir `docs/CAHIER_DES_CHARGES_RESEAU.md` pour ses spécifications. Deux
ajouts pour l'exposer en HTTP :

### 2.1 `app/api.py`

- **`MonitoringState`** : regroupe la configuration, la boucle
  (`MonitoringLoop`), et fait tourner cette dernière dans un **thread
  Python dédié** — `run_forever` (app/main.py) est bloquant et n'a pas de
  notion de rythme réel, donc l'API pilote elle-même l'avancée du temps
  simulé (`seconds_per_tick`, 2 secondes réelles par pas de 5 minutes
  simulées).
- **`DEFAULT_API_CONFIG`** : historique de calibration volontairement
  court (40 jours), distinct de `AppConfig.defaults()` (90 jours). Avec
  l'historique par défaut, la sélection de modèle déclenche un ajustement
  Holt-Winters sur chaque sortie au démarrage — plusieurs secondes,
  parfois sans converger (observé en développement). Comme le `lifespan`
  FastAPI attend la calibration avant de répondre aux requêtes, ça
  bloquait le démarrage de l'API. Un déploiement réel peut toujours passer
  `AppConfig.defaults()` explicitement à `MonitoringState`.
- **Endpoints** : voir le README pour la liste. Tous en lecture seule sauf
  `POST /monitoring/{start,stop}` et `PUT /config`.
- **`PUT /config`** ne modifie à chaud que les seuils du moteur d'incidents
  (`open_score`, `close_score`, `warning_score`, durées) et
  `alerts.critical_score`. Reconstruit un `IncidentConfig` complet pour
  que les validateurs (§21 : `close_score < warning_score < open_score`)
  s'appliquent avant toute prise d'effet.

### 2.2 Extensions à `storage/repository.py` et `incidents/manager.py`

- `Repository.get_anomalies`, `get_forecasts`, `list_incidents`,
  `get_incident` : lectures nécessaires à l'API, absentes du backend
  d'origine (qui n'avait besoin que d'écrire et de lister les incidents
  actifs).
- `SqlAlchemyRepository.__init__` : `connect_args={"check_same_thread": False}`
  pour SQLite — la boucle de monitoring (thread dédié) et les requêtes
  HTTP (thread de l'event loop FastAPI) accèdent à la même base depuis des
  threads différents. Chaque opération garde une session courte
  (`with self._session()`), ce qui reste sûr ; un déploiement à fort
  trafic HTTP concurrentiel gagnerait néanmoins à passer sur un vrai SGBD
  serveur (PostgreSQL) plutôt que SQLite.
- `IncidentManager.update_config` : reconfiguration à chaud, appelée par
  `PUT /config`. N'affecte que les décisions futures — un incident déjà
  ouvert continue d'évoluer sans être recalculé rétroactivement.
- `MonitoringLoop.forecast_ahead` : prévision à la demande au-delà de
  l'instant courant, à partir du modèle déjà calibré, sans rien persister
  — alimente la courbe "Prédiction" du dashboard (`GET
  /outputs/{id}/forecast/ahead`).

## 3. Frontend

### 3.1 `src/api.js`

Client HTTP minimal (`fetch`), une fonction par endpoint. Lit l'URL de
base depuis `VITE_API_BASE_URL` (voir `.env.example`) plutôt que de la
coder en dur.

### 3.2 `src/Dashboard.jsx`

Identité visuelle "Signal Room" reprise des maquettes précédentes (palette
fonctionnelle où la couleur code le statut, typographie à deux rôles,
silence visuel des sorties normales). Différences liées à la connexion
réelle :

- **Polling plutôt que simulation** : `/outputs`, `/incidents` et
  `/monitoring/status` toutes les 4 s ; le graphe détaillé d'une sortie
  dépliée toutes les 5 s ; le tableau comparatif toutes les 15 s (plus
  coûteux : jusqu'à 24 requêtes par rafraîchissement, une par fenêtre
  temporelle × sortie, voir 3.3).
- **Chargement paresseux** : les courbes Baseline/Prédiction ne sont
  interrogées (`/forecast`, `/forecast/ahead`) que si la case correspondante
  est cochée — un vrai bénéfice de la connexion réelle, impossible à
  espérer d'une simulation qui générait tout de toute façon.
- **`EmptyRange`** : si une plage demandée (ex. "1 mois") ne renvoie que 0
  ou 1 point, un message explicite s'affiche plutôt qu'un graphe vide ou
  une division par zéro dans le calcul d'échelle. Voir §4 pour pourquoi
  c'est un cas normal, pas un bug.
- **`SettingsPanel`** envoie réellement `PUT /config` (bouton "Envoyer à
  l'API", état de chargement, message d'erreur si l'API renvoie 422). Les
  seuils de coloration du tableau restent en revanche purement locaux
  (aucun endpoint ne les concerne) — précisé dans l'interface elle-même.

### 3.3 Le tableau comparatif, coûteux par nature

Chaque cellule (sortie × fenêtre temporelle × métrique) a besoin de la
valeur la plus proche d'un instant cible et de celle un pas avant, pour
calculer la variation. Sans endpoint dédié "valeur la plus proche d'un
instant", le frontend appelle `GET /outputs/{id}/history` avec une petite
fenêtre autour de chaque instant cible (`fetchNearest`) : 4 fenêtres × 2
appels (valeur + valeur précédente) × 3 sorties = 24 requêtes par
rafraîchissement. Acceptable pour un tableau de 3 sorties rafraîchi toutes
les 15 secondes ; à revoir (endpoint dédié côté API) si le nombre de
sorties grandit significativement.

## 4. Contrainte assumée : pas d'historique avant le démarrage

Une maquette purement simulée peut fabriquer un mois de données fictives
instantanément. Un vrai système ne le peut pas : `GET
/outputs/{id}/history` ne renvoie que ce que `MonitoringLoop` a réellement
traité et stocké depuis que l'API tourne. Avec le rythme accéléré actuel
(2 s réelles / 5 min simulées) :

| Plage demandée | Temps réel de fonctionnement nécessaire |
|---|---|
| 12 h | ~5.5 minutes |
| 1 jour | ~11.5 minutes |
| 1 semaine | ~1.1 heure |
| 1 mois | ~4.8 heures |

C'est un comportement correct d'un système de monitoring réel, pas une
limite à cacher — d'où le message explicite plutôt qu'un graphe cassé
(§3.2). `MockDataSource.load_history` peut bien fabriquer une plage
arbitraire dans le passé (utilisé pour la calibration initiale), mais
cette donnée synthétique n'est jamais persistée : seule la donnée
réellement passée par la boucle de monitoring l'est.

## 5. Vérifications effectuées

- Suite de tests complète (146 tests backend, 37 tests frontend) exécutée depuis la structure finale
  du projet (imports vérifiés depuis la racine).
- `npm run build` du frontend depuis la structure finale.
- Backend démarré en conditions réelles (`uvicorn`, pas seulement
  `TestClient`) ; chaque endpoint vérifié par requêtes HTTP réelles
  (`curl`), y compris les cas d'erreur (`404` sortie/incident inconnu,
  `422` configuration incohérente, `400` fenêtre temporelle invalide) et
  les en-têtes CORS.
- Formes de données comparées champ par champ entre les réponses réelles
  de l'API et ce que `Dashboard.jsx`/`api.js` attendent — notamment le
  format des timestamps (`Date.toISOString()` produit toujours un suffixe
  `Z`, jamais de `+`, donc pas de piège d'encodage d'URL côté navigateur,
  contrairement à un test manuel avec `datetime.isoformat()` de Python où
  ce piège existe réellement et a été rencontré puis résolu ici).

## Recalibration, alertes, rétention, évaluation

- **Recalibration périodique (§32)** — `MonitoringLoop._maybe_recalibrate` : à l'échéance de
  `monitoring.recalibration_interval`, les modèles sont réajustés en excluant les périodes couvertes par
  un incident confirmé (`list_incidents_overlapping`). Si plus de la moitié de l'historique d'une sortie
  serait exclue, son modèle actuel est conservé. Un échec ne stoppe pas la boucle.
- **Alertes (§27)** — `Alert`/`AlertEvent` (`models.py`), table `alerts`, `GET /incidents/{id}/alerts`
  et `GET /outputs/{id}/alerts`. Pas de canal externe (webhook…) : hors périmètre V1 (§38).
- **Rétention** — `Repository.purge_before` supprime mesures, prévisions et anomalies anciennes ; les
  incidents et alertes sont conservés.
- **Évaluation (§33)** — `network_monitor/evaluation/` : `metrics.py` (fonctions pures), `harness.py`
  (pipeline complet sur la suite de scénarios par défaut), CLI `python -m network_monitor.evaluation`.
- **Instantanés** — `GET /outputs/{id}/snapshots` : comparatif temporel calculé côté serveur ; la
  référence est la dernière mesure de la sortie (une mesure a un pas de retard sur l'horloge).
- **Schéma** — `create_all` n'ajoute pas de colonnes aux tables existantes : `SqlAlchemyRepository`
  applique une migration additive (colonnes nullables uniquement, sinon erreur explicite → Alembic).
- **`Incident.score`** est le pic du score depuis l'ouverture ; `Incident.current_score` est le dernier
  score (celui dont dérive `severity`).
