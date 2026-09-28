# Revue complète du projet — Network Monitor

**Date de la revue :** 28 septembre 2026
**Périmètre :** `network_monitor/` (backend Python/FastAPI), `frontend/` (React/Vite), `docs/`, outillage
**Méthode :** lecture intégrale du code source, exécution de la suite de tests, profiling (`cProfile`), inspection du schéma SQLite et des `EXPLAIN QUERY PLAN`, `npm install` / `npm run build` à froid, tests d'API réels (CORS, authentification, bornes de requête, injection)

---

## 1. Résumé exécutif

Le projet est **solide sur le fond et faible sur l'industrialisation**. L'architecture est remarquablement distinguée : séparation stricte des couches, contrats abstraits (`DataSource`, `Repository`, `ForecastModel`), documentation qui rattache chaque décision à une section du cahier des charges, tests de scénarios de bout en bout. C'est un bon travail d'ingénierie.

Mais le projet **n'est pas publishable en l'état** : une installation à froid du frontend échoue, la suite de tests est rouge, et la documentation affirme des vérifications qui ne sont plus vraies.

### Tableau de synthèse

| Axe | Note | Verdict |
|---|---|---|
| Sécurité | 🔴 3/10 | Aucune authentification + CORS ouvert sur toutes les méthodes = contrôle total à distance |
| Performance | 🔴 4/10 | 1,5 s par tick de monitoring, dominées par `fsync` SQLite ; 24 requêtes HTTP par rafraîchissement de tableau |
| Fiabilité / concurrence | 🟠 4/10 | Course sur l'horloge simulée, redémarrage de la boucle sans `join()`, sources non thread-safe |
| Qualité du code | 🟡 6/10 | Code propre et bien commenté, mais surface de configuration fictive et données perdues à l'écriture |
| Tests | 🔴 3/10 | 54 passent / 12 échouent / 5 erreurs ; 14 minutes d'exécution ; suite non déterministe |
| Design frontend | 🟢 7/10 | Direction artistique cohérente et assumée, mais interactions et responsive à reprendre |
| Accessibilité | 🟠 4/10 | Boutons de repli cassés au clavier, focus invisible, aucun landmark ni titre de niveau 1 |
| Outillage / packaging | 🔴 2/10 | Pas de `.gitignore`, pas de CI, pas de lint, dépendances non pinnées, install frontend cassée |
| Documentation | 🟡 6/10 | très bonne substance, mais contient des affirmations désormais fausses |

**Décision recommandée : ne pas publier avant de traiter les 4 blocages P0 (§2).** Le reste peut suivre en lots.

---

## 2. Blocages (P0) — à corriger avant toute publication

### 🔴 P0-1 — `npm install` échoue : le frontend n'est pas installable à froid

**Fichier :** `frontend/package.json:11-19`

`vite@^8.3.1` est exigeant sur `@vitejs/plugin-react`, qui ne déclare que `vite@^4 || ^5 || ^6 || ^7` en peer dependency.

```
npm error code ERESOLVE
While resolving: @vitejs/plugin-react@4.7.0
Found: vite@8.3.1
Could not resolve dependency:
peer vite@"^4.2.0 || ^5.0.0 || ^6.0.0 || ^7.0.0" from @vitejs/plugin-react@4.7.0
```

Vérifié : `npm install` **et** `npm ci` échouent tous les deux sur un clone neuf (testé dans un répertoire temporaire avec le `package-lock.json` du projet). Le `build` ne passe que parce que le `node_modules` local a été installé avec `--force`/`--legacy-peer-deps`.

**Impact :** toute personne clonant le dépôt — et toute CI — échoue à l'étape 2 du « Démarrage rapide » du README.

**Correction :** passer `@vitejs/plugin-react` à `^5.0.0` (compatible Vite 8), ou redescendre `vite` à `^7`. Le build produit déjà un avertissement de dépréciation qui indique la voie attendue :

```
[vite:react-babel] We recommend switching to `@vitejs/plugin-react-oxc` for improved performance.
```

### 🔴 P0-2 — Aucune authentification + CORS `*` sur toutes les méthodes

**Fichier :** `network_monitor/app/api.py:124-130`

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Vérifié en conditions réelles (`TestClient`, en-tête `Origin: https://evil.example.com`) :

| Vérification | Résultat |
|---|---|
| `access-control-allow-origin` | `*` |
| `access-control-allow-methods` | `DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT, **QUERY**` |
| `PUT /config` cross-origin, **sans aucun identifiant** | `200` — seuils modifiés (`open_score` → `0.9`) |
| `POST /monitoring/stop` sans identifiant | `200` — monitoring arrêté |

**Impact :** n'importe quel site web peut, depuis le navigateur d'un utilisateur dont le poste a le backend en local, **arrêter silencieusement la surveillance** et **reconfigurer les seuils de détection**. Le scénario d'attaque est trivial : un utilisateur visitant une page tierce executes un `fetch('http://127.0.0.1:8000/monitoring/stop', {method:'POST', mode:'cors'})`. Le CORS `*` ne s'y oppose pas. C'est un vecteur d'attaque réel, pas théorique — le « DNS rebinding » et les extensions de navigateur rendent le `localhost` atteignable depuis une page distante.

La note de sécurité en tête de `api.py:8-10` reconnaît le risque (« à restreindre à l'origine réelle avant tout déploiement ») mais le risque est structurel, pas documentaire.

**Correction (par ordre d'importance) :**
1. Restreindre `allow_origins` à une liste explicite issue de la configuration (et non `*`).
2. Retirer les méthodes non utilisées : ne garder que `GET` et `PUT /config`.
3. Ajouter une dépendance d'authentification (jeton API) sur `PUT /config`, `POST /monitoring/{start,stop}`.
4. Lier le serveur à `127.0.0.1` par défaut et documenter que l'exposition réseau exige une configuration explicite.

Ce point est **non négociable** avant une publication publique : le code sera lu, et l'absence d'authentification sur une API de contrôle sera le premier point relevé.

### 🔴 P0-3 — La suite de tests est rouge (54 / 66)

Le README (`README.md:21`) et `docs/ARCHITECTURE.md:144-145` annoncent « 66 tests » et « Suite de tests complète (66 tests) exécutée ». Exécution réelle :

| Fichier | Passés | Échecs | Erreurs | Durée |
|---|---|---|---|---|
| `test_models.py`, `test_detection.py`, `test_forecasting.py`, `test_incidents.py` | 42 | 0 | 0 | 28 s |
| `test_repository.py` | 0 | 0 | **5** | — |
| `test_scenarios.py` | 0 | **11** | 0 | **709 s** |
| `test_api.py` | 12 | **1** | 0 | 92 s |
| **Total** | **54** | **12** | **5** | **~14 min** |

**Cause 1 — descripteur de fichier SQLite jamais libéré.** Les 16 échecs/erreurs (`test_repository.py:30`, `test_scenarios.py:45`) sont tous un `PermissionError` au teardown :

```
PermissionError: [WinError 32] Le processus ne peut pas accéder au fichier car le fichier
est utilisé par un autre processus: 'C:\...\tmp9s_itlfz.db'
```

`SqlAlchemyRepository` ne dispose jamais de son moteur. Le `with self._session():` ferme la session mais pas le pool de connexions, donc le fichier SQLite reste verrouillé sous Windows. Ajouter une méthode `dispose()` et l'appeler dans les fixtures (et à l'arrêt de l'application) règle les 16.

**Cause 2 — un test dépend du temps de latence réel.** `test_api.py:97-118` (`test_scripted_incident_is_visible_end_to_end`) injecte un scénario puis `time.sleep(2.5)`. Or la boucle met **1,5 s par tick** (voir §4) : 2,5 s ne permettent qu'**un seul tick**, alors que l'ouverture d'incident exige 3 ticks consécutifs au-dessus de `open_score` (`minimum_duration = 10 min`). Le test est structurellement incapable de passer. C'est le symptôme direct de la lenteur, pas d'un défaut de logique de détection.

**Cause 3 — tests dépendants du temps via `sleep` et d'un état global.** `api_module.state` est une variable globale de module que le fixture remplace. Combiné aux `time.sleep()` et à `seconds_per_tick = 0.05` réellement inopérant, la suite est **non déterministe** : elle peut passer ou échouer selon la charge machine.

**Correction :** (a) `dispose()` + fixtures sur `tmp_path` avec `yield` ; (b) remplacer `time.sleep()` par une attente active sur un état attendu (ex. boucle d'attente sur `/incidents` avec un timeout) ; (c) isoler l'état via une factory plutôt qu'un patch de module ; (d) bannir `tempfile.mktemp` (déprécié, vulnérable à une course sur le nom de fichier) au profit de `tmp_path`.

### 🔴 P0-4 — Pas de `.gitignore` : 586 Mo de `.venv` et 2,5 Mo de base à publier

**Aucun `.gitignore` n'existe à la racine.** `git add .` embarquerait :

| Élément | Taille | Problème |
|---|---|---|
| `.venv/` | **586 Mo** | Environnement virtuel complet, non reproductible |
| `network_monitor.db` | 2,5 Mo | Données d'exécution, contient des dates **simulées jusqu'au 2026-10-04** (postérieures à aujourd'hui) |
| `frontend/node_modules/` | ~50 Mo | Régénérable |
| `frontend/.env` | 189 o | Doit être versionné via `.env.example` uniquement |
| `__pycache__/`, `*.pyc`, `.pytest_cache/` | — | Bruit |

**Correction :** ajouter un `.gitignore` (Python + Node), retirer `network_monitor.db` de l'index, et ne versionner que `frontend/.env.example`.

---

## 3. Sécurité — revue détaillée

### Points positifs ✅

| Contrôle | État | Vérification |
|---|---|---|
| Injection SQL | ✅ Protégé | Tout passe par l'ORM SQLAlchemy, requêtes paramétrées. Testé avec `OUT_001'--` → `404` propre |
| Traversée de répertoire | ✅ Protégé | `/outputs/..%2F..%2Fetc/history` → `404` |
| Filtrage d'incidents | ✅ Protégé | `status=["' OR 1=1--"]` → `200` avec 0 résultat, pas d'erreur SQL |
| Désérialisation | ✅ Sûr | `yaml.safe_load` exclusively, jamais `yaml.load` ; sérialisation `evidence` en JSON, jamais `pickle` |
| Validation d'entrée | ✅ Bonne | Contraintes Pydantic `ge`/`le` sur tous les scores, `Field(min_length=1)` sur `output_id` |
| Secrets en clair | ✅ Aucun | Aucun mot de passe, token ou clé dans le code, la config ou le `.env` |
| Validation des seuils | ✅ Bonne | `close_score < warning_score < open_score` appliqué **avant** prise d'effet, via reconstruction complète de l'`IncidentConfig` (`api.py:301-334`) — pas d'affectation directe qui contournerait les règles |

Le travail de sécurisation est réel et bien pensé. Le problème est qu'il s'arrête à la couche applicative : **il n'y a pas de couche d'authentification ni de restriction de surface d'attaque.**

### Points à corriger 🟠

| # | Gravité | Problème | Emplacement |
|---|---|---|---|
| S-1 | 🔴 | Aucune authentification sur les endpoints de contrôle | `api.py:284-334` |
| S-2 | 🔴 | CORS `*` + `allow_methods=["*"]` (cf. P0-2) | `api.py:124-130` |
| S-3 | 🟠 | **Fenêtre temporelle non bornée côté serveur** — `since`/`until` acceptent n'importe quelle plage. Un `GET /history?since=2010-01-01` renvoie l'intégralité de la table. Aucun `max_span` → amplification mémoire (déni de service trivial) | `api.py:193-236` |
| S-4 | 🟠 | **Aucune limitation de débit** sur les endpoints, combinée à S-3 | `api.py` global |
| S-5 | 🟡 | `/docs` et `/openapi.json` exposés sans restriction → divulgation de la surface d'API complète | `api.py:118-123` |
| S-6 | 🟡 | Les messages d'erreur `PUT /config` renvoient `str(exc)` brut (erreur Pydantic complète, plusieurs centaines de caractères). Utiliser `exc.errors()` | `api.py:325-326` |
| S-7 | 🟡 | **Datetime naïf accepté silencieusement** : un client envoyant `since=2020-01-01T00:00:00` (sans fuseau) obtient `200`, et `_to_utc()` appelle `.astimezone()` sur un naïf, qui **interprète l'heure locale** → résultats silencieusement faux. Doit être rejeté en `422` | `repository.py:31-35`, `api.py:193` |
| S-8 | 🟡 | `@import url('https://fonts.googleapis.com/...')` dans une balise `<style>` : requête vers un tiers à chaque chargement (adresse IP divulguée), sans `Content-Security-Policy`, et bloquante au rendu | `Dashboard.jsx:621` |
| S-9 | 🟡 | Les dépendances ne sont pas pinnées (`>=` partout) : le build n'est pas reproductible, et une version future compromise est tirée automatiquement. `polars` et `scikit-learn` ne sont **jamais importés** (0 référence) → surface d'attaque inutile | `requirements.txt` |
| S-10 | 🟢 | `tempfile.mktemp()` (déprécié, course sur le nom de fichier) au lieu de `tmp_path` | `test_repository.py:26`, `test_scenarios.py:29` |

### Note : le verrouillage de base est absent

`check_same_thread=False` est correctement posé et **commenté** (`repository.py:137-141`), mais le mode « pool par défaut » n'a pas de timeout. Sous charge, les écritures du thread de monitoring et les lectures HTTP concurrentes peuvent se bloquer ou lever `OperationalError: database is locked`. Le passage à PostgreSQL est déjà identifié dans `ARCHITECTURE.md:60-66` — c'est la bonne direction, il faut juste formaliser la limite.

---

## 4. Performance — revue détaillée

### 🔴 Perf-1 — 1,5 s par tick de monitoring, dominé par `fsync` SQLite

**C'est le problème de performance n°1 du projet.** Profilage `cProfile` de 6 ticks (Windows, disque local) :

```
ncalls  tottime  cumtime  function
    47    7.823    7.825  {method 'commit' of 'sqlite3.Connection' objects}
─────────────────────────────────────────────────────────────────
     6    0.003    9.094  run_tick
```

**7,82 s sur 9,09 s (86 %) sont passés dans `commit()`.** Décomposition mesurée :

- Calibration initiale (40 jours, 34 560 échantillons, 3 sorties) : **3,5 s** — correct.
- **Chaque tick : 1,57 s à 2,27 s** —emianement injustifié.

**Cause : jusqu'à 10 transactions SQLite par tick.** Un tick écrit 3 mesures (`save_samples`, 1 commit), puis pour chacune des 3 sorties : `save_forecasts` (1 commit) + `save_anomalies` (1 commit) + `upsert_incident` (1 commit). Soit 7 à 10 `fsync` séparés à **~166 ms chacun**.

**Correction mesurée — deux lignes :**

```python
with self._engine.connect() as conn:
    conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
```

| Configuration | 12 ticks | Par tick |
|---|---|---|
| Actuel | 18,85 s | **1 571 ms** |
| WAL + `synchronous=NORMAL` | 0,83 s | **69 ms** |

**Gain : 23×.**(`journal_mode=WAL` supprime en plus le verrou d'écriture global, ce qui améliore aussi la concurrence lecture/écriture entre le thread de monitoring et les requêtes HTTP.)

**Complément recommandé :** regroupez les écritures d'un tick dans **une seule transaction** (un `session_scope()` par tick plutôt que par appel de méthode). Cela réduit le nombre de `fsync` de 7-10 à 1 et rend le tick atomique — ce qui est de toute façon la sémantique attendue.

### 🟠 Perf-2 — Le tableau comparatif provoque 24 requêtes HTTP par rafraîchissement

**Fichier :** `frontend/src/Dashboard.jsx:296-328`

Chaque cellule (sortie × fenêtre × métrique) appelle `fetchNearest` deux fois (valeur + valeur un pas avant) via `GET /outputs/{id}/history` :

```
4 fenêtres × 2 appels × 3 sorties = 24 requêtes, toutes les 15 s
```

Pour 12 cellules affichées. L'ARCHITECTURE reconnaît le problème (`ARCHITECTURE.md:108-118`) mais le contourne.

Aggravant : **aucun `AbortController`**, donc quand l'API met 1,5 s par tick (P0-3), les requêtes s'empilent. Sur une API lente, 24 requêtes non annulées × 15 s de période = accumulation.

**Correction :** un endpoint dédié `GET /outputs/{id}/summary?at=<iso>&offset=<iso>` qui calcule les agrégats **côté serveur** — 1 requête par sortie au lieu de 8. Le serveur a déjà les données en base, la comparaison de timestamps n'a rien à faire dans le navigateur.

### 🟠 Perf-3 — Aucune limitation de la taille des séries SVG

`DetailChart` construit des chemins SVG **sans sous-échantillonnage** (`Dashboard.jsx:157-162`) :

```js
const pathFor = (pts, yFn) => pts.map(...).join(" ");
```

Sur la plage « 1 mois » (5 min de pas), une sortie représente **8 640 points** par courbe, 3 courbes visibles, **rafraîchi toutes les 5 secondes**. `pathFor` s'exécute sur chaque rendu, et chaque `setHistory` avec un nouveau tableau force la reconstruction complète des 3 chemins. Sur une plage longue, le graphe devient saccadé et coûteuse.

**Correction :** agrégation par bucket temporel (LTTB ou moyenne par pixel) quand `points.length > largeur du graphe en px * 2`. Le sous-échantillonnage doit préserver les pics — c'est tout l'intérêt d'un graphe de supervision.

### 🟡 Perf-4 — Cascade de re-rendus toutes les 4 secondes

`Dashboard.jsx:450-454` : `ensureOutputDefaults` appelle **trois** `setState` avec des objets **neuf à chaque fois**, depuis `refreshTopLevel` qui tourne toutes les 4 s.

```js
setExpandedCharts((prev) => { const next = { ...prev }; ...; return next; });  // identité toujours nouvelle
setVisibility((prev) => { ... });   // idem
setChartRange((prev) => { ... });   // idem
```

Les valeurs sont identiques au contenu précédent, mais React compare les **identités**, donc chaque appel force un re-rendu de tout l'arbre. Il faut retourner `prev` quand rien n'a changé.

Deux points positifs à noter : `SummaryTable` utilise `outputIds.join(",")` comme dépendance de `useCallback` (`Dashboard.jsx:322`), ce qui empêchefortunately une fuite d'`setInterval` en boucle ; et `DetailChart` démonte proprement son intervalle quand la sortie est repliée (`Dashboard.jsx:557`), donc pas de pollution. Ces deux pièges ont été évités.

### 🟡 Perf-5 — Croissance de base de données non bornée

Aucun mécanisme de rétention. Le `network_monitor.db` fourni contient déjà **5 748 lignes** dans chacune des tables `raw_measurements` / `forecasts` / `anomalies`, et **440 incidents**, pour ~7 jours simulés. En fonctionnement continu (un tick toutes les 2 s), c'est ~12 600 lignes/heure/table, sans plafond.

**Correction :** une purge périodique (tâche de fond ou déclenchée par le tick) sur les tables de mesures/prévisions/anomalies au-delà d'une rétention configurable, **en préservant** les incidents et leurs preuves.

### 🟠 Perf-6 — `evidence` croît sans borne, réécrit intégralement à chaque tick

`manager.py:255-267` : `incident.evidence.append(...)` à chaque tick, et `repository.py:195-216` réécrit le **blob JSON complet** via `session.merge()` à chaque append.

Mesuré sur la base fournie : `length(evidence)` moyen **706 octets**, maximum **4 478 octets**. Pour un incident de 90 minutes (18 ticks), cela produit une amplification d'écriture **O(n²)**.

**Correction :** borner la liste (garder les N premières + les N dernières preuves, comme le ferait un ring buffer), ou normaliser les preuves dans une table séparée comme le fait déjà `IncidentEvidence` pour les incidents eux-mêmes.

### 🟡 Perf-7 — Index manquants sur `incidents`

`EXPLAIN QUERY PLAN` sur la base réelle :

```sql
-- GET /outputs (toutes les 4 s)
select * from incidents where status in ('suspect','open','stale');
→ SCAN incidents

-- GET /incidents (toutes les 4 s)
select * from incidents order by last_seen_at desc limit 50;
→ SCAN incidents
→ USE TEMP B-TREE FOR ORDER BY
```

Les tables de mesures sont correctement indexées (auto-index de clé primaire composite exploitée : `SEARCH ... USING INDEX sqlite_autoindex_raw_measurements_1`), mais `incidents` n'a **aucun index** sur `status` ni sur `last_seen_at`. Les deux requêtes sont donc en coût linéaire croissant sur une table qui grossit en continu — **et le dashboard les interroge toutes les 4 secondes**.

**Correction :** `Index("ix_incidents_status", "status")` et `Index("ix_incidents_last_seen", "last_seen_at")`.

---

## 5. Fiabilité et concurrence 🟠

### Conc-1 — Course sur l'horloge simulée

**Fichier :** `network_monitor/app/api.py:96-104`

```python
def _run(self) -> None:
    while not self._stop.is_set():
        self.now = self.now + interval        # ← écriture
        self.loop.run_tick(self.now)          # ← et les handlers lisent state.now
```

`self.now` est **écrit par le thread de monitoring et lu par les handlers HTTP**, sans verrou. Pire : `now` est avancé **avant** que le tick n'ait rien écrit en base. Une requête `/outputs` peut donc lire un `state.now` en avance sur les données effectivement persistées, etFenêtre renvoie un `latest_sample` plus ancien que « maintenant ».

**Correction :** avancer `self.now` **après** `run_tick` (et non avant), et le protéger par un `threading.Lock` — ou mieux, le faire Derivé de la dernière donnée persistée plutôt que d'un compteur mural.

### Conc-2 — `stop()` ne fait pas de `join()` : deux boucles peuvent tourner simultanément

**Fichier :** `network_monitor/app/api.py:92-94`

```python
def stop(self) -> None:
    self._stop.set()
    self.running = False      # aucun _thread.join()
```

Un `POST /monitoring/stop` suivi d'un `POST /monitoring/start` immédiat (exactement ce que fait `test_api.py:90-94`) crée un **nouveau thread alors que l'ancien est peut-être encore dans `run_tick`**. Les deux écrivent alors en parallèle.

Aggravant, `MockDataSource.get_latest` (`data/mock.py:229-237`) mute `self._clock` sans verrou, et `self._rng` (source de bruit numpy) est partagé. **Les deux ne sont pas thread-safe.**

**Correction :** `self._stop.set(); self._thread.join(timeout=...)` dans `stop()`, et un `threading.Lock` dans la source.

### Conc-3 — `MockDataSource` et `ForecastModel` non thread-safe

Au-delà du cas ci-dessus, `_buckets` (dict modifié lors du `fit`) est lu depuis `predict`. `update_config` (`api.py:330-332`) mute `state.config` pendant que le thread de monitoring lit `self._config.incident.open_score` (`main.py:129`). Le couplage implicite — `MonitoringLoop._config` **est** l'objet `state.config`, donc la mutation de `state.config.incident` dans `update_config` a un effet de bord non intentionnel sur le `PersistenceTracker` — est fragile : un jour où `update_config` ne serait plus appelé, la détection utiliserait silencieusement des seuils périmés.

**Correction :** passer les seuils explicitement à la boucle, et documenter l'invariant ou le supprimer.

### Conc-4 — Fuite mémoire de `DataValidator._seen_keys`

`data/source.py:62` : `self._seen_keys: set[tuple[str, datetime]] = set()` n'est **jamais purgé**. Chaque mesure reçue y entre définitivement. Sur une source réelle en fonctionnement continu, c'est une croissance monotone sans limite.

**Correction :** purger les entrées au-delà d'une fenêtre glissante (`last_seen_ts - N * interval`).

---

## 6. Qualité du code et architecture 🟡

### Points forts ✅

- **Découpage des couches réellement propre** : `app/` (orchestration), `data/` (source), `analysis/` (modèles), `incidents/` (état), `storage/` (persistance). Les contrats abstraits (`DataSource`, `Repository`, `ForecastModel`) sont bien tenus.
- **Documentation remarquable** : chaque module a une docstring qui **rattache ses choix au cahier des charges** (`§14`, `§21`, `§31`…). Le code est navigable par un lecteur qui ne l'a pas écrit.
- **Contrats de données stricts** : Pydantic v2 partout, avec validation croisée (`bounds_are_consistent`, `timestamps_are_ordered`) et contraintes de domaine (`timestamp_must_be_timezone_aware`).
- **Choix de normalisation justifié** : `min(1, écart/référence)` au lieu d'un z-score, avec l'argumentaire en commentaire. C'est un vrai choix d'ingénierie, pas un arbitraire.
- **Gestion correcte des trous de données** : la logique `STALE` / suspension de persistance (`manager.py:212-237`) est juste et non triviale.

### 🟠 Q-1 — La surface de configuration ment sur ce qu'elle contrôle

`AppConfig` expose un grand nombre de paramètres **qui n'ont aucun effet runtime**. Vérifié par recherche de références hors `config.py` :

| Champ config | Utilisé ? |
|---|---|
| `detection.traffic` | ❌ 0 référence |
| `detection.loss.enabled` | ⚠️ seul `loss_std_floor` est lu, jamais `.enabled` |
| `detection.isolation_forest` | ❌ 0 référence |
| `detection.extended_features` | ❌ 0 référence |
| `forecast.horizon` | ❌ 0 référence |
| `forecast.models` | ❌ 0 référence |
| `history.minimum` | ❌ 0 référence (seul `preferred` est lu) |
| `alerts.enabled` | ❌ 0 référence |
| `alerts.warning_score` | ❌ 0 référence |

Un opérateur qui règle `forecast.models: ["holt_winters"]` ou `detection.isolation_forest.enabled: true` croira avoir changé le comportement du système. **Il ne se passe rien, et rien ne le signale.** C'est le pire type de configuration : celle qui ment silencieusement.

**Correction :** soit implémenter le câblage, soit (recommandé à ce stade) **supprimer** les champs inopérants et documenter les capacités réelles.

### 🟠 Q-2 — Le système d'alertes §27 est du code mort

`IncidentManager.process()` renvoie `(list[Alert], Incident | None)`. Les deux appelants font :

```python
# main.py:111
_, incident = self._incident_manager.process(gap_result)
# main.py:134
_, incident = self._incident_manager.process(...)
```

**La liste d'alertes est systématiquement jetée.** La dataclass `Alert`, la méthode `_alert()`, et la logique de sévérité associée sont donc écrites, testéesimplicitement, et jamais exercées en production. `config.alerts.enabled` n'est jamais consulté (cf. Q-1).

Soit le §27 est implémenté jusqu'au bout (transport des alertes : webhook, log, SSE vers le frontend), soit c'est du code à supprimer. Le §27 du cahier des charges est explicitement dans le périmètre, donc la première option est la bonne.

### 🟠 Q-3 — Données perdues à l'écriture

`AnomalyResult` expose 7 champs. La table `anomalies` n'en stocke que 5 :

```
Colonnes réelles : output_id, timestamp, traffic_anomaly_score, loss_anomaly_score,
                   persistence_score, joint_anomaly_score, data_gap
```

`traffic_deviation_percent`, `traffic_out_of_interval` et `loss_deviation_points` sont **calculés à chaque tick puis abandonnés**. `get_anomalies()` (`repository.py:248-263`) reconstruit un `AnomalyResult` sans eux → **l'endpoint `/outputs/{id}/anomalies` renvoie `null` pour 3 de ses 7 champs documentés**, et `persistence_score` non plus côté `main.py:129` en pratique.

Un écart en pourcentage est précisément l'information qu'un exploitant veut lire dans un rapport d'anomalie. **Correction :** ajouter les colonnes manquantes.

### 🟡 Q-4 — `upsert_incident` réécrit l'historique complet à chaque tick

`main.py:143-144` persiste l'incident à **chaque** tick, y compris quand rien n'a changé de manière significative. Combiné à Q-6 (evidence), c'est de l'IO inutile. Écrire uniquement sur changement d'état réel (transitions `suspect`/`opened`/`recovering`/`closed`/`stale`) suffirait.

### 🟡 Q-5 — Approche de sélection de modèle (§11) méthodologiquement biaisée

`forecasting.py:300-311` :

```python
def _backtest_mae(model, train, validation, interval):
    horizon = interval * len(validation)           # ← toute la fenêtre d'un coup
    forecasts = model.predict(train[-1].timestamp, horizon, interval)
```

Sur une fenêtre de validation de 7 jours (2 016 pas), Holt-Winters est évalué sur un horizon **multi-pas de 2 016**, ce qui est catastrophiquement pessimiste pour un modèle à tendance amortie, alors que la Seasonal Baseline est evaluated avec un profil d'erreur quasi stationnaire. **Le critère favorise structurellement la baseline**, quel que soit le qualité réelle du signal.

**Correction :** backtest par glissement (*rolling-origin*), c'est-à-dire prédire chaque point de validation depuis le même point que la boucle le ferait en production (un pas d'avance), et moyenner les MAE. C'est plus coûteux mais c'est la seule façon d'évaluer honnêtement un modèle destiné à la prédiction à un pas.

### 🟡 Q-6 — Divers

| # | Problème | Emplacement |
|---|---|---|
| Q-6a | Import de `IncidentEvidence` **dans le corps de la fonction**, exécuté à chaque ligne lue — import circulaire évité par cowardise plutôt que par architecture | `repository.py:311` |
| Q-6b | API `session.query()` (héritage SQLAlchemy 1.x) au lieu de `select()` (2.0 idiomatique) | `repository.py:221+` |
| Q-6c | `except Exception: pass` autour d'une construction qui, comme le dit le commentaire, « ne peut pas échouer » — du code mort qui masque l'intention | `forecasting.py:274-277` |
| Q-6d | `time.sleep()` dans le thread de la boucle : occupe un thread pour rien, et `stop()` ne peut pas interrompre le sleep en cours | `api.py:104` |
| Q-6e | État global mutable `state = MonitoringState()` au niveau module : force le patch de module dans les tests | `api.py:107` |
| Q-6f | Pas de `pyproject.toml`, donc pas de `requires-python`, pas de métadonnées, pas de configuration `ruff`/`mypy` | racine |
| Q-6g | Le README recommande `pip install --break-system-packages` — mauvaise pratique qui encourages à contourner la protection de l'environnement Python | `README.md:41` |

### 🟢 Points positifs notables

- `yaml.safe_load` avec le raisonnement écrit en commentaire.
- Validation des seuils par **reconstruction** de l'objet plutôt que par affectation — la bonne façon de faire (`api.py:301-334`).
- `PersistenceTracker` reset à zéro en dessous du seuil plutôt que décroissance — correct.
- `_process_active_incident` : la gestion de `touched_incident` qui survit à la remise à `None` après fermeture est subtile et correctement commentée (`manager.py:111-118`).
- `PersistenceTracker` / `LossBaseline` sont des dataclasses séparées et testables, pas des monolithes.

---

## 7. Tests 🟠

### Manquements fonctionnels

| Domaine | Couvert ? |
|---|---|
| Modèles, détection, forecasting, incidents, repository | ✅ Bon |
| Scénarios end-to-end (10 types du §7) | ✅ Bon dans l'intention |
| **Validation `DataValidator`** (doublons, sortie inconnue, hors bornes, arrivée tardive) | ❌ **Aucun test** |
| **`DataValidator.missing_slots`** (génération de trous temporels) | ❌ **Aucun test** |
| **`_classify_type`** (classification des types d'incident) | ❌ Aucun test direct |
| **Backtesting / `select_model` au-dessus du seuil** | ❌ Seul le repli est testé ; le chemin Holt-Winters ne l'est jamais |
| **Validation YAML** (`AppConfig.from_yaml`) | ❌ Aucun test — une fonction de chargement de fichier sans un seul test |
| **Requête de plage temporelle invalide** | ⚠️ Partiel (`test_api.py:58`) |
| **`_parse_duration`** (12 formats) | ❌ Aucun test direct, alors qu'un bug y rend la configuration silencieusement fausse |

### Manquements non fonctionnels

- **Aucun test de charge** — le comportement sous 50 requêtes concurrentes est inconnu, et c'est précisément le contexte où le verrouillage SQLite apparaîtrait.
- **Aucun test de concurrence** — malgré Conc-1, Conc-2, Conc-3 qui sont des bugs réels.
- **Aucune couverture mesurée** — impossible de savoir si les 20 % de code non couvert contiennent les bugs.
- **`time.sleep()` comme synchronisation** — anti-pattern qui rend la suite non déterministe (cf. P0-3).

---

## 8. Frontend — design, UX, accessibilité

### 🟢 Direction artistique : solide

L'identité « Signal Room » est **cohérente et assumée** :
- Palette sombre à forte dominante verte, où **la couleur code la fonction** (état, série de données) et non la décoration. C'est le bon principe pour une console de supervision.
- Typographie à deux rôles claire : `Space Grotesk` (interface) / `JetBrains Mono` (données), avec `font-variant-numeric: tabular-nums` — les chiffres s'alignent verticalement dans le tableau, ce qui est exactement ce qu'il faut pour comparer des métriques.
- Le bruit de fond en `repeating-linear-gradient` (lignes de balayage) et l'animation de pulsation du point « live » créent une ambiance juste **sans nuire à la lisibilité**.
- Le silence visuel sur les sorties normales et la montée en intensité sur les actives (`is-active` avec halo `inset`) sont une bonne hiérarchie.
- **`prefers-reduced-motion` est respecté** pour l'animation de pulsation (`Dashboard.jsx:642`) — détail rare et appreciated.
- Le message `EmptyRange` (`Dashboard.jsx:79-87`) est un **excellent choix de conception** : plutôt que de masquer la limite (« vous n'avez pas d'historique »), il explique honnêtement pourquoi. C'est rare et cela se voit.

### 🟠 Responsive : absent

`@media` n'existe que pour `.settings-grid` (700 px). Partout ailleurs :
- `.nm-channel-head` a `gap: 24px` et des `min-width` cumulés de 130 + 100 + 140 + bouton ≈ **430 px** → débordement horizontal sous ~500 px de large.
- `.nm-header-meta` a `gap: 22px` et ne casse pas proprement.
- Le tableau est protégé par `min-width: 760px` + `overflow-x: auto`, ce qui est correct — **mais `overflow-x: auto` est appliqué sur `.nm-table-panel`, qui contient aussi `.nm-panel-head`** : le titre défile donc horizontalement avec le tableau. À corriger en déplaçant l'overflow sur un conteneur dédié.

Ajoutez au moins un palier `max-width: 720px` qui empile les blocs de `.nm-channel-head` et réduit les gaps.

### 🔴 A11y-1 — Les boutons de repli sont cassés au clavier (bug fonctionnel)

**Fichier :** `frontend/src/Dashboard.jsx:540, 552-555, 575-579, 585-589`

Structure sur les trois panneaux repliables :

```jsx
<div className="nm-channel-head" onClick={...} role="button" tabIndex={0}
     onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") toggleChart(id); }}>
  ...
  <button type="button" onClick={(e) => { e.stopPropagation(); toggleChart(id); }}>−</button>
</div>
```

Deux problèmes distincts :

1. **Double déclenchement.** Sur le `<button>` interne, l'appui sur `Entrée` déclenche : (a) un `click` sur le bouton → `toggleChart` ; (b) le `keydown` **remonte** (bulle) jusqu'au `div` parent → `toggleChart` **encore une fois**. Les deux appels s'annulent : **le bouton ne fait rien au clavier.** (À la souris, `stopPropagation` sur le `click` empêche le problème — d'où un bug qui ne se voit pas en test manuel.)
2. **Espace non neutralisé.** Sur le `div` en `role="button"`, appuyer sur `Espace` fait défiler la page **et** bascule le panneau. Il manque `e.preventDefault()`.

**Correction :** supprimer `role`/`tabIndex`/`onKeyDown` du `div` et ne garder que le vrai `<button>` (qui gère déjà Entrée et Espace nativement), ou faire un vrai `<button>` unique. Ne jamais imbriquer un contrôle interactif dans un `role="button"`.

### 🟠 A11y-2 — Focus invisible sur la plupart des contrôles

Seuls `.nm-channel-head:focus-visible` et `.nm-collapse-btn:focus-visible` ont un `outline` (`:651`, `:660`). **Aucun style de focus** sur :
- `.range-btn` (les 4 sélecteurs de plage)
- `.legend-chip` (les 4 bascules de courbes)
- `.settings-reset` (le bouton d'envoi de la configuration)

Un utilisateur clavier ne sait pas où il est. **Échec WCAG 2.4.7 (Focus Visible)**. Correction : une règle globale
```css
.nm-root :is(button, [role="button"], input):focus-visible {
  outline: 2px solid var(--trace); outline-offset: 2px;
}
```

### 🟠 A11y-3 — Structure de titres et landmarks absents

- **Aucun `<h1>`** sur le dashboard principal : le titre est un `<div className="nm-title">` (`Dashboard.jsx:524`). Le `<h1>` n'existe que dans l'écran d'erreur. → **WCAG 1.3.1**.
- Hiérarchie incohérente : `<h2>` pour les panneaux, `<h3>` pour les groupes de configuration, mais `<span className="settings-field-label">` pour les libellés de champs (devraient être des `<label>` associés — ils le sont partially via `<label>` wrapper, ce qui est correct, mais le `<h3>` "Moteur d'incidents" et "Tableau comparatif" sont au même niveau que les panneaux qu'ils détaillent).
- **Aucun landmark** : la page entière est une pile de `<div>`. Ni `<header>`, ni `<main>`, ni `<section>`, ni `<nav>`. → navigation au clavier très difficile. → **WCAG 1.3.1 / 2.4.1**.
- **Pas de lien d'évitement** (*skip link*).

### 🟠 A11y-4 — Le graphe SVG est invisible pour les lecteurs d'écran

`<svg viewBox=...>` (`:171`) n'a ni `role="img"`, ni `<title>`, ni `aria-label`. Un lecteur d'écran ne descrira **rien** du graphe — qui est pourtant l'information principale de la page. L'information existe partiellement dans le tableau, mais pas les séries baseline/prédiction ni les bornes de l'intervalle prédictif.

**Correction :** `role="img"` + un `aria-label` synthétique (« Trafic et pertes de OUT_001 sur 12 heures, 3 courbes visibles »), et un tableau de données alternatif accessible.

### 🟡 A11y-5 — Absence de région live pour les changements d'état

C'est un **dashboard de supervision** : l'apparition d'un incident est l'événement le plus important de la page, et elle n'est **pas annoncée**. Il manque un `aria-live="polite"` (ou `role="log"`) sur le journal d'incidents, avec annonce du nouvel élément seulement.

### 🟡 A11y-6 — Points mineurs

- Le `<button>` de configuration `<h3>` contient un `<button>` : même problème d'imbrication que A11y-1.
- `e.key === " "` devrait utiliser `e.code === "Space"` (plus robuste, et `key` peut être `"Spacebar"` sur d'anciens navigateurs).
- Aucune préférence de mouvement au-delà de la pulsation (pas de transition de `transform` ailleurs, donc le sujet est moot).
- `lang="fr"` correctement déclaré sur `<html>` ✅.

### 🟡 Design — axes du graphe qui débordent

`MARGIN.left = 50` px, mais le libellé d'axe est `formatMbps()` qui produit jusqu'à `"32.00 Gbps"` (10 caractères à `fontSize: 9` dans une police monospace ≈ 54 px) → **débordement hors du SVG**. Le libellé « maintenant » est positionné à `y = MARGIN.top - 2 = 10`, ce qui le coupe ou le chevauche avec `strokeDasharray`. Élargir `MARGIN.left` à 66 et ajouter `MARGIN.top` (ou un `textAnchor`/`dy` correct).

### 🟡 Design — pas d'interaction sur le graphe

Aucun survol, aucun tooltip, aucun crosshair, aucun zoom. Sur une console « oscilloscope », il est **impossible de lire une valeur exacte** sur une courbe. C'est la principale lacune fonctionnelle du design : les courbes sont décoratives plutôt qu'instrumentales. Un crosshair avec valeur au survol (et un focus clavier) est le changement au meilleur rapport effort/valeur de toute la section.

### 🟡 Design — `@import` Google Fonts dans une balise `<style>`

`Dashboard.jsx:621` : l'`@import` est **dans une constante template string** injectée via `<style>{BASE_STYLE}</style>`. Conséquences : requête vers Google à chaque montage (pas de préchargement, pas de `font-display` contrôlé), et le style est ré-injecté à chaque rendu. À déplacer dans `index.html` avec `<link rel="preconnect">`, ou auto-héberger (voir S-8).

### 🟡 Frontend — autres points

- **Aucun `ErrorBoundary`** : une exception de rendu blanche l'écran entier.
- **`console.error` nu** dans le `catch` de `DetailChart` (`:109`) : l'utilisateur ne voit **rien** quand le graphe échoue à se rafraîchir — il continue d'afficher des données potentiellement périmées sans le moindre indicateur de fraîcheur. Le dashboard n'affiche nulle part **l'âge de la dernière mise à jour réussie**.
- **`fetch` sans timeout ni `AbortController`** : une requête pendante reste pendante indéfiniment.
- **Aucun état de "dernière mise à jour"** ni indicateur de fraîcheur des données, alors que c'est la métrique la plus importante pour un outil de supervision.
- **Pas de gestion du 429/503** ni de backoff : en cas de surcharge, le dashboard continue de marteler l'API à fréquence constante (3, 9 et 24 requêtes selon les panneaux ouverts).
- **Pas de tests frontend** : aucun test unitaire, aucun test de composant, aucun test e2e. Pour un composant de 708 lignes avec de la logique asynchrone, de l'état dérivé et des intervalles, c'est un angle mort important.
- **Pas de TypeScript** : le projet manipule des payloads d'API non typés (`output.latest_sample?.traffic_mbps` sans vérification de forme). Une erreur de forme côté backend ne serait découverte qu'à l'exécution, via un `TypeError` dans le rendu. Un `openapi-typescript` généré depuis `/openapi.json` est à faible coût et à fort rendement ici.
- **Dépendances non pinnées** dans `package.json` (`^`), cohérent avec le problème côté Python.

---

## 9. Outillage, packaging et documentation 🔴

| Élément | État | Action |
|---|---|---|
| `.gitignore` | ❌ **Absent** | Créer (Python + Node + `.env` + `*.db`) — cf. P0-4 |
| `LICENSE` | ❌ **Absent** | Obligatoire pour un dépôt public. MIT ou Apache-2.0 |
| CI (`.github/workflows`) | ❌ **Absent** | `pytest` + `ruff` + `mypy` + `npm ci && npm run build` |
| Linter / formateur | ❌ **Absent** | `ruff` (rapide, couvre lint + format) — aucun `flake8`/`black`/`eslint`/`prettier` |
| Typage statique | ❌ **Absent** | `mypy --strict` — le code est déjà largement annoté, le gain serait immédiat |
| `pre-commit` | ❌ Absent | hooks lint + typage + tests |
| `pyproject.toml` | ❌ **Absent** | `requires-python`, métadonnées, config outils |
| `requirements.txt` pinné | ❌ `>=` partout | Reproductibilité absente |
| Séparation dev/prod | ❌ Mélangé | `pytest`, `httpx` sont des deps de prod |
| `polars`, `scikit-learn` | ❌ **Jamais importés** (0 référence) | Supprimer (~50 Mo et de la surface d'attaque) |
| `CONTRIBUTING.md` | ❌ Absent | — |
| `SECURITY.md` | ❌ Absent | À créer dès qu'il y a une politique d'authentification |
| `CHANGELOG.md` | ❌ Absent | — |
| `CODE_OF_CONDUCT` | ❌ Absent | — |
| `.editorconfig` | ❌ Absent | — |

### ⚠️ La documentation contient des affirmations désormais fausses

**`README.md:21`** — « `tests/` 66 tests (unitaires, scénarios, API) » : lu comme « 66 tests qui passent ». Or 12 échouent et 5 erreurient.

**`docs/ARCHITECTURE.md:144-147`** :
> - Suite de tests complète (66 tests) exécutée depuis la structure finale du projet.
> - `npm run build` du frontend depuis la structure finale.

Les deux sont **faux** aujourd'hui. C'est le point le plus important de cette section : publier un dépôt dont la documentation affirme des vérifications que l'état courant contredit, c'est nuire à la crédibilité du projet dès la première visite.

Le reste de la documentation est en revanche **excellent** : `ARCHITECTURE.md` explique honnêtement le compromis « pas d'historique avant le démarrage » (§4), le coût du tableau comparatif (§3.3), et le choix `DEFAULT_API_CONFIG` avec sa raison (`api.py:33-43`). C'est rare et précieux.

**Autres corrections :**
- `README.md:41` : retirer `--break-system-packages` (mauvaise pratique ; utiliser un venv).
- `README.md:53` : `cp .env.example .env` — commande Unix, cassée sous Windows. Documenter les deux formes.
- `README.md:91` : « une plage 1 mois nécessite environ 5 heures » — le tableau de `ARCHITECTURE.md:128-133` dit 4,8 h. Harmoniser.
- Préciser la version de Python requise (non déclarée nulle part).

---

## 10. Plan de remédiation priorisé

### Lot 0 — avant publication (bloquant)

| # | Action | Fichiers | Temps |
|---|---|---|---|
| 1 | Aligner `@vitejs/plugin-react` sur Vite 8, régénérer le lockfile | `frontend/package.json` | 5 min |
| 2 | `.gitignore` complet + retirer `network_monitor.db` et `.venv` | `.gitignore` | 5 min |
| 3 | `LICENSE` (MIT) + `pyproject.toml` avec `requires-python` | racine | 10 min |
| 4 | `dispose()` sur le moteur + appel dans les fixtures | `repository.py`, 2 fixtures | 15 min |
| 5 | Corriger les 12 tests rouges (dépend de #4 et #6) | `test_repository.py`, `test_scenarios.py`, `test_api.py` | 45 min |
| 6 | `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` | `repository.py:__init__` | 10 min |
| 7 | Corriger le README et `ARCHITECTURE.md` (supprimer les vérifications fausses) | docs | 15 min |

**Effort total : ~2 h. Résultat : dépôt installable, tests verts, documentation honnête.**

### Lot 1 — sécurité et performances (fortement recommandé)

| # | Action | Fichiers | Temps |
|---|---|---|---|
| 8 | CORS sur liste blanche d'origines, méthodes restreintes | `api.py:124` | 20 min |
| 9 | Bornes sur la fenêtre temporelle (`max_span`) + rejet des datetimes naïfs | `api.py`, `repository.py` | 30 min |
| 10 | Une seule transaction par tick | `main.py`, `repository.py` | 30 min |
| 11 | Index sur `incidents.status` et `incidents.last_seen_at` | `repository.py` | 5 min |
| 12 | `stop()` → `join()` ; verrou sur `MockDataSource` | `api.py`, `mock.py` | 20 min |
| 13 | Purgation périodique des mesures (rétention configurable) | `repository.py` | 45 min |

**Effort total : ~3 h. Gain : tick à ~70 ms (23×), API non pilotable par un tiers, base bornée.**

### Lot 2 — qualité

| # | Action | Temps |
|---|---|---|
| 14 | `ruff` + `mypy` en CI ; corriger ce qu'ils trouvent | 1 h |
| 15 | Supprimer la configuration inopérante ou la câbler | 1 h |
| 16 | Persister `traffic_deviation_percent` / `traffic_out_of_interval` / `loss_deviation_points` | 30 min |
| 17 | Implémenter le transport des alertes §27, ou supprimer le code | 2 h |
| 18 | Tests manquants : `DataValidator`, `missing_slots`, `from_yaml`, `_parse_duration` | 2 h |
| 19 | `dispose()` + `delete()` sur `Repository` pour rendre les tests isolables | 20 min |

### Lot 3 — frontend

| # | Action | Temps |
|---|---|---|
| 20 | Corriger le double déclenchement clavier (A11y-1) | 20 min |
| 21 | Focus visible global (A11y-2) | 10 min |
| 22 | `<h1>`, landmarks, skip link (A11y-3) | 45 min |
| 23 | `role="img"` + `aria-label` sur le SVG (A11y-4) | 15 min |
| 24 | `aria-live` sur le journal d'incidents (A11y-5) | 15 min |
| 25 | Endpoint d'agrégat serveur : 24 requêtes → 3 (Perf-2) | 1 h |
| 26 | Sous-échantillonnage des séries longues (Perf-3) | 1 h |
| 27 | `ErrorBoundary`, `AbortController`, timeout, indicateur de fraîcheur | 1 h |
| 28 | Paliers responsive + marges du graphe | 45 min |
| 29 | Crosshair + tooltip sur le graphe | 1 h |
| 30 | Générer les types depuis `openapi.json` (TypeScript) | 1 h |

---

## 11. Ce qui mérite d'être dit

Ce projet est **nettement au-dessus de la moyenne** sur la conception. Le découpage en couches, les contrats abstraits, la traçabilité de chaque décision au cahier des charges, la gestion des trous de données avec suspension de persistance, la reconstruction de configuration pour valider les seuils avant prise d'effet — ce sont des choix d'ingénieur expérimenté, pas des choix d'étudiant qui assemble des briques.

Ce qui manque, ce n'est pas la compétence : c'est la **passage en production**. Les cinq axes qui font la différence entre un bon exercice et un logiciel maintenable — authentification, gestion de la concurrence, discipline des performances, non-répudiation des tests, reproductibilité du build — sont précisément ceux qui manquent. Et ce sont des axes qu'on acquiert en livrant, pas en concevant.

Le plus vicieux n'est pas l'absence d'authentification, c'est `docs/ARCHITECTURE.md:144` qui affirme une suite de tests verte. **La documentation qui ment est plus dangereuse que le code qui manque** : elle fait perdre du temps à celui qui essaie d'aider. Le README a d'ailleurs bien fait de documenter honnêtement le compromis « pas d'historique avant le démarrage » — la même honnêteté appliquée aux tests et à l'installation aurait évité l'essentiel de ce rapport.

---

## Annexe — Commandes de vérification

Toutes les commandes ont été exécutées depuis `C:\Users\FRANCIS\Desktop\network-monitor-full`.

```powershell
# Tests — 54 passés, 12 échecs, 5 erreurs, ~14 min
.\.venv\Scripts\python.exe -m pytest network_monitor/tests/ -q

# Test isolé le plus lent : 93 s pour un seul test
.\.venv\Scripts\python.exe -m pytest network_monitor/tests/test_scenarios.py::test_normal_behavior_never_opens_incident -q

# Erreur teardown : descripteur SQLite non libéré
# PermissionError: [WinError 32] ... 'C:\...\tmp9s_itlfz.db'

# Install frontend à froid : échec
cd frontend; npm install        # ERESOLVE — @vitejs/plugin-react@4.7.0 vs vite@8.3.1
cd frontend; npm ci             # ERESOLVE — idem

# Gain de performance mesuré (12 ticks)
#   sans PRAGMA : 18,85 s (1 571 ms/tick)
#   WAL + synchronous=NORMAL : 0,83 s (69 ms/tick)   -> 23x

# Requêtes incidents sans index (base réelle)
#   select * from incidents where status in (...)  -> SCAN incidents
#   select * from incidents order by last_seen_at    -> SCAN + USE TEMP B-TREE

# CORS (TestClient, Origin: https://evil.example.com)
#   access-control-allow-origin : *
#   access-control-allow-methods: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT, QUERY
#   PUT /config sans identifiant      -> 200
#   POST /monitoring/stop sans identifiant -> 200

# Dépendances jamais importées : polars, scikit-learn, scipy -> 0 référence
```

**Environnement de la revue :** Windows 11, Python 3.x via `.venv`, Node 24.19.0, npm 12.0.2.
Versions installées : FastAPI 0.141.1, Starlette 1.7.0, Pydantic 2.13.5, SQLAlchemy 2.1.1, numpy 2.5.3, statsmodels 0.15.0, httpx 0.28.1, pytest 9.1.1.
