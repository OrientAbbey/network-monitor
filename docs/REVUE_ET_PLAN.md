# network-monitor — Revue complète et plan d'action

**Commit analysé :** `6b883b6` (branche `master`, 28/09/2026)
**Périmètre :** backend `network_monitor/`, frontend `frontend/`, `docs/`, `REVUE.md`, outillage
**Environnement de vérification :** Linux, Python 3.12.3, Node 22.22.2, npm 10.9.7, dépendances Python et npm aux dernières versions stables

---

## 0. Méthode : ce qui a été vérifié, et comment

Je n'ai pas seulement relu `REVUE.md` : j'ai cloné le dépôt, lu tout le code source, puis **exécuté** ce qui pouvait l'être. Chaque constat porte une étiquette :

| Étiquette | Signification |
|---|---|
| ✅ **Mesuré** | reproduit par une commande ou un script dans cet environnement |
| 📖 **Lu** | établi par lecture du code, non exécuté |
| ⚠️ **Non reproduit** | affirmé par `REVUE.md`, mais je n'ai pas pu le reproduire ici |

**Limites (à garder en tête) :** pas de navigateur (aucun test d'accessibilité ou de rendu réel), environnement Linux (les problèmes propres à Windows ne sont pas reproductibles), pas de vraies données réseau (uniquement le Mock).

---

## 1. Verdict

L'architecture est bonne : couches propres, contrats abstraits (`DataSource`, `Repository`, `ForecastModel`), documentation qui rattache chaque décision au cahier des charges, gestion soignée des trous de données. Sur ce point, `REVUE.md` a raison.

Mais **le projet a des défauts fonctionnels plus graves que ceux listés dans `REVUE.md`**, et deux d'entre eux invalident le produit tel qu'il est démontré :

1. **Le dashboard n'affiche presque aucune donnée** : le frontend interroge le backend avec l'heure du navigateur, alors que les données sont sur une horloge simulée qui avance 150× plus vite (§2.2, N-1). Après 35 s : 16 échantillons en base, **1 seul visible** dans le graphe.
2. **Le modèle Holt-Winters est figé** : sa prévision ne dépend pas de l'instant demandé (N-2). Il est aujourd'hui masqué parce que l'API utilise un historique de 40 j (< 45 j → baseline saisonnière), mais la configuration par défaut du cahier des charges (90 j) l'activerait.
3. **La détection est bruyante et fragmente les incidents** : sur 7 jours de trafic *normal*, 491 enregistrements d'incident dont 5 `OPEN` (faux positifs). Un scénario intermittent de 60 min produit 13 enregistrements et 4 `OPEN` au lieu d'un incident (le §22 du cahier des charges l'interdit explicitement).
4. **Aucune authentification** sur les endpoints de contrôle, et le seul correctif CORS ne suffit pas (§3, S-1).
5. **Des fonctionnalités du cahier des charges sont absentes** : transport des alertes (§27), recalibration (§32), module d'évaluation (§33), features (§9), abstraction `AnomalyDetector` (§36) (§2.3).

**Décision recommandée :** ne pas publier ni présenter en démonstration avant les phases 0 et 1 du plan (≈ 2,5 jours). Le reste peut suivre par lots.

### Ce que `REVUE.md` a bien vu et que je confirme

`npm ci` échoue (✅), CORS `*` + aucune authentification (✅), `PUT /config` et `POST /monitoring/stop` ouverts (✅), dépendances non pinnées et inutilisées (✅ `polars`, `scikit-learn`, `scipy` : 0 import), configuration qui ment (📖), alertes calculées puis jetées (📖), 3 champs d'anomalie perdus à l'écriture (📖), absence d'index, de purge, de `dispose()` (📖), bugs de clavier et d'accessibilité du frontend (📖).

### Ce qui est faux, obsolète ou à nuancer dans `REVUE.md`

| Point de `REVUE.md` | Réalité constatée |
|---|---|
| **P0-3** « suite de tests rouge (54/66, 14 min) » | ✅ **66/66 passent en ~42 s sous Linux.** Les échecs semblent propres à Windows (verrou de fichier SQLite, `fsync` lent, causes indiquées par `REVUE.md` et cohérentes avec le code, mais non reproductibles ici) et à des tests dépendants du temps. Le problème est réel pour l'auteur, mais c'est un problème de **portabilité et de fragilité**, pas une logique cassée. |
| **P0-4** « pas de `.gitignore`, 586 Mo de `.venv` » | ✅ **Corrigé** : `.gitignore` et `LICENSE` existent, aucun `.db`/`.venv` n'est versionné. Section à supprimer. |
| **Perf-1** « 1,5 s par tick, gain 23× avec WAL » | ⚠️ **Non reproduit** : 16 ms par tick ici, avec ou sans WAL (le coût du `fsync` dépend du disque et du système). WAL reste recommandé pour la concurrence lecture/écriture, mais le chiffre « 23× » n'est pas universel. |
| **§3** « Validation des seuils ✅ `close < warning < open` appliqué » | ✅ **Faux.** Seul `close_score < open_score` est vérifié. `warning_score=0.9` avec `open_score=0.7` est accepté, et `PUT /config {"warning_score": 0.99}` renvoie `200` (N-6). |
| **S-7** « datetime naïf accepté → 200 » | ✅ Plus nuancé : `since` naïf **seul** → **HTTP 500** (`TypeError`, comparaison naïf/aware) ; `since` **et** `until` naïfs → 200 avec interprétation silencieuse en heure locale. |
| **Q-6d/Conc-1…** | 📖 Exact, mais secondaires face à N-1 et N-2. |
| Texte du fichier | Plusieurs mots sont corrompus (« emianement », « etFenêtre », « erreurient », « Derivé », « executes »…). À relire avant de le republier, ou à remplacer par ce document. |

---

## 2. Constats détaillés

### 2.1 Constats de `REVUE.md` conservés (pas de nouvelle analyse nécessaire)

P0-1 (npm), P0-2 (CORS/auth), S-3 (plage non bornée), S-4 (rate limit), S-5 (`/docs` exposé), S-6 (`str(exc)`), S-8 (Google Fonts), S-9 (deps), Perf-2 à Perf-7, Conc-1 à Conc-4, Q-1 à Q-6, A11y-1 à A11y-6, responsive, crosshair, ErrorBoundary. Ils sont repris dans le plan (§3) avec leur identifiant d'origine.

### 2.2 Nouveaux constats (absents de `REVUE.md`)

#### 🔴 N-1 — Le frontend et le backend n'ont pas la même horloge ✅ Mesuré

`Dashboard.jsx:97, 135, 300` construisent toutes les fenêtres avec `new Date()` (heure du navigateur). Le backend, lui, avance `state.now` de 5 minutes simulées toutes les 2 s réelles (`api.py:96-104`). Seule l'horloge d'en-tête (`Dashboard.jsx:528`) utilise `simulated_now`.

Mesure sur un serveur réel après ~35 s :

```
heure réelle (UTC)                                     18:16:31
horloge simulée                                        19:35:59   (avance : 1,3 h)
échantillons vus par le frontend (fenêtre = heure réelle)       : 1
échantillons réellement en base (fenêtre = horloge simulée)     : 16
/forecast/ahead commence à 19:40:59 → axe du graphe étiré de 1,9 h vers le futur
```

Conséquences : chaque échantillon n'est visible que lorsque l'heure réelle atteint son horodatage simulé (l'échantillon n°100, produit après 200 s, n'apparaît qu'au bout de ~8 h) ; le graphe est presque vide ; la courbe « Prédiction » est décalée dans le futur et écrase l'axe ; le tableau comparatif compare des instants sans données. Le message `EmptyRange` masque le vrai problème en le présentant comme une limite d'historique.

**Correctif :** le client ne doit jamais fabriquer d'instants. Voir tâche P1-1.

#### 🔴 N-2 — `HoltWintersModel.predict()` ignore `from_time` ✅ Mesuré

`forecasting.py:230-233` appelle `self._fit_result.forecast(n_steps)`, qui prévoit toujours **à partir de la fin de la série d'ajustement**, puis rebaptise les horodatages avec `from_time + i*interval`. Mesure (série sinusoïdale journalière, 10 jours) :

```
predict(fin)        = 1000.0
predict(fin + 6 h)  = 1000.0
predict(fin + 12 h) = 1000.0     → identiques
```

De plus, le modèle n'est jamais mis à jour avec les nouvelles observations. En production, dès que Holt-Winters est retenu, la « prévision » d'un pas est **constante** : elle ne suit plus le cycle journalier, donc génère de faux positifs ou masque de vraies anomalies. Ce chemin n'est pas exercé aujourd'hui (l'API et les tests utilisent 40 jours de calibration, sous le seuil de 45 jours), et **aucun test ne couvre la branche Holt-Winters de `select_model`**.

Le backtest (`_backtest_mae`) est correct par hasard (la validation suit immédiatement l'entraînement), ce qui masque le défaut.

#### 🟠 N-3 — Bruit d'incidents et fragmentation ✅ Mesuré

Mock, seed 3, calibration 40 j, baseline saisonnière :

| Expérience | Résultat |
|---|---|
| 7 jours **normaux** (2 016 pas × 3 sorties) | **491 incidents créés** : 486 `SUSPECT` refermés « sans confirmation » + **5 `OPEN`** (faux positifs) |
| Scénario `traffic_drop` 40 min | 3 enregistrements, 1 `OPEN` (délai d'ouverture 10 min) |
| Scénario `progressive_degradation` 60 min | 10 enregistrements, 3 `OPEN` |
| Scénario `intermittent_incident` 60 min | 13 enregistrements, **4 `OPEN`** |

Trois causes dans `manager.py` : (1) un `SUSPECT` qui retombe sous `warning_score` est persisté comme incident `CLOSED` (bruit), (2) aucun mécanisme de fusion : dès qu'un incident se ferme, une nouvelle anomalie en crée un autre, alors que le §22 exige « un incident, pas quatre », (3) les seuils n'ont jamais été calibrés contre une mesure (le module §33 n'existe pas, cf. §2.3). Le test `test_normal_behavior_never_opens_incident` ne couvre que 2 h.

#### 🟠 N-4 — `DataValidator.missing_slots` n'est pas idempotent ✅ Mesuré

À chaque tick, la fonction régénère **tous** les créneaux manquants depuis le dernier échantillon valide. Pour un trou de 5 pas : 1 + 2 + 3 + 4 + 5 = **15 résultats émis au lieu de 5**. Chaque doublon ajoute une preuve `data_gap` à l'incident (`manager.py:_handle_data_gap`), ce qui aggrave Perf-6 (croissance quadratique du blob `evidence`).

#### 🟠 N-5 — Classification des types d'incident ✅ Mesuré

`_classify_type(0.8, 0.8, +60.0)` (trafic en **hausse** + pertes) renvoie `traffic_drop_with_high_loss`. Le cas 4 du §25 (trafic ↑ + pertes ↑ = congestion) est donc inexprimable. Par ailleurs `PROGRESSIVE_DEGRADATION` et `INTERMITTENT_DEGRADATION` (§23) ne sont **jamais produits** : les scénarios correspondants sont étiquetés `traffic_drop` ou `traffic_drop_with_high_loss`.

#### 🟠 N-6 — Configuration : validation incomplète et état global partagé ✅ Mesuré

- `warning_score` n'est jamais comparé à `open_score`/`close_score` (voir §1). `critical_score` n'est pas validé non plus.
- `MonitoringState` fait `self.config = config or DEFAULT_API_CONFIG` : `state.config is DEFAULT_API_CONFIG` est `True`. Un `PUT /config` **modifie la constante de module**, donc toute nouvelle instance (tests, redémarrage à chaud) hérite des seuils modifiés (vérifié : `DEFAULT_API_CONFIG.incident.open_score` passe à `0.9`).

#### 🟠 N-7 — `since` naïf → erreur 500 ✅ Mesuré

`GET /outputs/{id}/history?since=2020-01-01T00:00:00` : `TypeError: can't compare offset-naive and offset-aware datetimes` (`_parse_window`, `api.py`). Une entrée client invalide ne doit jamais produire un 500.

#### 🟡 N-8 — `calibrate()` recharge tout l'historique pour chaque sortie 📖

`main.py:64-66` appelle `load_history` **dans** la boucle sur les sorties, puis filtre par `output_id` : chaque appel génère l'historique de *toutes* les sorties. Coût O(n²) en nombre de sorties (négligeable à 3 sorties, bloquant à 300).

#### 🟡 N-9 — Horloge simulée non alignée, reprise incohérente ✅ Mesuré / 📖

Les horodatages sont de la forme `19:30:59.372727Z` : `state.now` part de `datetime.now()` sans arrondi à la grille de 5 minutes. Au redémarrage, la base contient des données « futures » (l'horloge simulée dépasse l'heure réelle), puis l'horloge repart de l'heure réelle : les nouvelles écritures (`session.merge`) écrasent ou entrelacent les anciennes.

#### 🟡 N-10 — L'historique de calibration n'est pas persisté 📖

La calibration charge 40 jours d'historique mais ne l'écrit pas dans `raw_measurements`. Le dashboard ne peut donc rien montrer d'antérieur au démarrage (le README documente « ~5 h pour la plage 1 mois »). En mode démonstration, c'est corrigeable à faible coût.

#### 🟡 N-11 — Qualité des tests 📖 / ✅ Mesuré

- Les tests de scénarios n'affirment que « un incident s'est ouvert » : ni nombre d'incidents, ni type (progressif, intermittent), ni durée, ni taux de faux positifs.
- `PROGRESSIVE_RECOVERY` et `COMBINED` : « s'exécute sans exception » uniquement.
- `TestClient` + `httpx` émet un `StarletteDeprecationWarning` (Starlette 1.7 recommande `httpx2`).
- `tempfile.mktemp` (déprécié, condition de concurrence) dans deux fichiers.

#### 🟡 N-12 — Analyse statique ✅ Mesuré

`ruff` (règles par défaut) : **41 alertes**, 26 corrigeables automatiquement. `mypy` : **39 erreurs**, dont des vraies :

- `main.py:87` : liste de `TrafficSample | None` passée là où `TrafficSample` est attendu ;
- `forecasting.py:206` : `_fit_result` peut être `None` (accès à `fittedvalues`) ;
- `repository.py` : modèles SQLAlchemy déclarés en style 1.x (`Column`), non typés ; passer aux colonnes `Mapped[...]` (SQLAlchemy 2.x) supprimerait toute la famille d'erreurs ;
- `AppConfig(history={...})` passe un `dict` là où un `HistoryConfig` est attendu (fonctionne par coercition Pydantic, échoue au typage).

#### 🟡 N-13 — Divers

- `frontend/src/api.js` : les `output_id` ne sont pas encodés dans les URL (`/outputs/${outputId}`) : utiliser `encodeURIComponent`. 📖
- Le README recommande toujours `pip install --break-system-packages`. 📖
- Le rapport `REVUE.md` (49 Ko) est plus long que le code backend utile et contient des passages corrompus : remplacer par un `docs/REVIEW.md` court et daté. 📖

### 2.3 Écarts par rapport au cahier des charges (fonctionnalités manquantes)

| § | Exigence | État | Preuve |
|---|---|---|---|
| **27 / 28** | Alertes : transport, contenu (output, type, sévérité, début, durée, attendu, observé, écart, pertes, raisons), anti-spam | ❌ **Non livré** : `IncidentManager.process()` renvoie des `Alert`, jetées par les deux appelants ; la dataclass `Alert` n'a pas les champs du §27 | `main.py:111, 134` |
| **32** | Recalibration périodique configurable, sans réapprendre sur les périodes anormales | ❌ Absent | aucune référence dans le code |
| **33** | Évaluation : Precision, Recall, F1, FPR, Detection/Closure Delay, Duration Error ; MAE, RMSE, sMAPE, couverture d'intervalle ; hors période de calibration | ❌ Absent (`ground_truth()` existe mais n'a aucun consommateur) | pas de module `evaluation` |
| **9.1** | Features V1 : `rolling_std_1h`, `hour`, `day_of_week` calculées et stockées | ❌ Non calculées ni stockées | tables sans ces colonnes |
| **9.2** | `analysis/features_extended.py` (features étendues, désactivées par défaut) | ❌ Absent | — |
| **17 / 36** | `AnomalyDetector` (interface), `StatisticalDetector`, `IsolationForestDetector` optionnel | ❌ Aucune abstraction ; la détection est un ensemble de fonctions ; `detection.isolation_forest` et `scikit-learn` sont fictifs | Q-1, N-13 |
| **23** | Types `PROGRESSIVE_DEGRADATION`, `INTERMITTENT_DEGRADATION` (+ congestion, §25 cas 4) | ❌ Jamais produits | N-5 |
| **31** | `TEMPORAL_GAP` | ❌ Défini, jamais utilisé | — |
| **35** | Configuration YAML documentée | ⚠️ `from_yaml` sans test ni fichier d'exemple ; plusieurs champs sans effet | Q-1 |
| **34** | Tests obligatoires | ⚠️ Partiels : pas de `DataValidator`, `missing_slots`, `from_yaml`, `_parse_duration`, ni de branche Holt-Winters | §7 de `REVUE.md`, confirmé |
| — | Source de données réelle | ❌ Uniquement le Mock | `data/` |
| — | Déploiement (Dockerfile, CI, migrations de schéma) | ❌ Absents | — |

---

## 3. Plan de correction et d'ajout de fonctionnalités

Estimations en jours-personne, à titre indicatif. Chaque tâche a un **critère de validation** vérifiable.

**Ordre recommandé : 0 → 1 → 3a → 2 → 3b → 4 → 5 → 6.** Le banc d'évaluation (3a) est construit *avant* de toucher aux seuils et à la logique d'incident : sans lui, on règle à l'aveugle.

### Phase 0 — Débloquer l'installation et fiabiliser la suite (≈ 0,5 j)

| ID | Action | Fichiers | Critère de validation |
|---|---|---|---|
| P0-1 | Passer `@vitejs/plugin-react` à `^6.1.1` (compatible Vite 8) et React/React-DOM à `^19.3.0`. Régénérer le lockfile. *Vérifié : `npm install` + `npm run build` réussissent, 0 vulnérabilité ; le fonctionnement du dashboard sous React 19 dans un navigateur n'a pas été testé.* | `frontend/package.json`, lockfile | `npm ci && npm run build` vert sur clone neuf |
| P0-2 | Ajouter `pyproject.toml` (`requires-python`, dépendances avec bornes basses, extras `dev`), retirer `polars`, `scikit-learn`, `scipy` (0 import), séparer `pytest`/`httpx2`/`ruff`/`mypy` en dépendances de dev, produire un lockfile aux dernières versions stables | racine | `pip install -e .[dev]` sur environnement vide |
| P0-3 | `SqlAlchemyRepository.dispose()` + appel dans les fixtures et au `lifespan` ; fixtures sur `tmp_path` avec `yield` ; supprimer `tempfile.mktemp` | `repository.py`, `test_repository.py`, `test_scenarios.py`, `test_api.py` | suite verte **sous Windows et Linux** |
| P0-4 | Remplacer les `time.sleep` par une attente active avec délai maximal (`wait_until(cond, timeout)`) ; remplacer le patch de module `api_module.state` par une fabrique `create_app(state)` | `test_api.py`, `api.py` | 3 exécutions consécutives identiques ; aucun `sleep` dans les tests |
| P0-5 | Corriger les affirmations fausses : « 66 tests » (préciser l'environnement), retirer `--break-system-packages` (venv), documenter `copy .env.example .env` pour Windows, harmoniser 4,8 h / 5 h | `README.md`, `docs/ARCHITECTURE.md` | relecture croisée README ↔ commandes réellement exécutées |
| P0-6 | Remplacer `REVUE.md` par ce document (ou un extrait daté) ; supprimer les sections devenues fausses (P0-4, `.gitignore`) | `REVUE.md` | aucune affirmation contredite par l'état du dépôt |

### Phase 1 — Corriger les défauts fonctionnels de fond (≈ 2 j)

**P1-1 — Base de temps unique (N-1).** Le serveur est la seule source du « maintenant ».

- API : ajouter `server_time` (UTC réel) à `MonitoringStatus` ; faire accepter `?span=12h` aux endpoints d'historique (fenêtre relative à `state.now`), en plus de `since`/`until`.
- Frontend : récupérer `simulated_now` **avant** tout appel de données, en dériver `since`/`until`, le faire circuler comme prop (`Dashboard` → `DetailChart`, `SummaryTable`), et ne plus appeler `new Date()` pour construire des fenêtres.
- Critère : avec un serveur démarré depuis 60 s, le graphe 12 h affiche tous les échantillons persistés ; `forecast/ahead` prolonge la courbe de 35 min juste après le dernier point ; test frontend (Vitest) qui simule un serveur en avance de 10 h.

**P1-2 — Holt-Winters aligné et mis à jour (N-2).**

```python
# fit(): conserver la fin de l'ajustement
self._fit_end = history[-1].timestamp

# predict(): décalage en pas depuis la fin de l'ajustement
offset = round((from_time - self._fit_end) / interval)
if offset < 0:
    raise ValueError("from_time antérieur à la fin de l'ajustement")
n_steps = max(1, int(horizon / interval))
forecast_log = self._fit_result.forecast(offset + n_steps)[offset:]
# l'incertitude doit croître avec (offset + i), pas seulement i
```

Ce correctif rend la prévision juste tant que le modèle est frais ; il ne remplace pas la mise à jour avec les nouvelles observations : la recalibration périodique (P4-3) est indispensable. À évaluer aussi : un modèle à espace d'états qui accepte de nouvelles observations sans réajustement complet (`statsmodels` ETS ; à valider par test avant adoption).

- Critère : test avec 60 jours d'historique où `predict(t)` suit le cycle journalier sur 24 h (erreur relative < 10 % sur une série synthétique) ; test de `select_model` qui exerce réellement le chemin Holt-Winters.

**P1-3 — `missing_slots` idempotent (N-4).** Conserver un curseur `_gap_cursor[output_id]` (dernier créneau déjà émis) et démarrer à `max(dernier_valide, curseur) + interval`. Critère : test « trou de 5 pas → exactement 5 résultats sur 5 ticks » ; le blob `evidence` ne croît plus quadratiquement.

**P1-4 — Types d'incident (N-5).** Ajouter `IncidentType.CONGESTION` (trafic ↑ + pertes ↑) ; corriger `_classify_type` ; produire `PROGRESSIVE_DEGRADATION` (pente positive du score sur les N dernières preuves, sans saut brutal) et `INTERMITTENT_DEGRADATION` (≥ 2 alternances au-dessus/en dessous du seuil pendant l'incident). Critère : un test paramétré par type de scénario du §7 affirme le **type** attendu.

**P1-5 — Configuration (N-6).** Valider `close < warning < open` dans `IncidentConfig` (validateur de modèle, pas de champ) ; valider `critical_score ≥ open_score` ; ne plus muter la constante : `self.config = (config or DEFAULT_API_CONFIG).model_copy(deep=True)` ; renvoyer `exc.errors()` au lieu de `str(exc)`. Critère : `PUT {"warning_score": 0.99}` → 422 ; deux `MonitoringState` successifs ne partagent aucun état.

**P1-6 — Entrées de l'API (N-7, S-3, S-7).** Rejeter tout datetime naïf en 422 ; borner `since/until` (`max_span` configurable, 31 jours par défaut, 422 au-delà) ; limiter la taille de réponse (`limit` + décimation côté serveur pour les plages longues). Critère : plus aucun 500 sur entrée invalide (test de type *fuzz* léger sur les paramètres).

### Phase 2 — Sécurité et robustesse (≈ 2,5 j)

**P2-1 — Authentification d'abord, CORS ensuite.** Un CORS restrictif **ne protège pas** `POST /monitoring/stop` : c'est une requête « simple » (sans corps), que le navigateur envoie même si la réponse est masquée. Il faut donc un jeton exigeant un en-tête personnalisé (ce qui force un *preflight*) :

```python
def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.api_token  # variable d'environnement NM_API_TOKEN, jamais en dur
    supplied = (authorization or "").removeprefix("Bearer ")
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401)

@app.put("/config", dependencies=[Depends(require_token)])
@app.post("/monitoring/stop", dependencies=[Depends(require_token)])
@app.post("/monitoring/start", dependencies=[Depends(require_token)])
```

En complément : `allow_origins` lu depuis `NM_CORS_ORIGINS` (défaut `http://localhost:5173`), `allow_methods=["GET","PUT","POST"]`, `allow_headers=["Authorization","Content-Type"]`. Le frontend lit le jeton depuis une variable d'environnement de build pour le mode démo, ou depuis un champ de saisie en mémoire. Critère : `PUT /config` et `POST /monitoring/*` sans jeton → 401 ; depuis une origine non autorisée → bloqué.

**P2-2 — Concurrence (Conc-1 à Conc-3, Q-6d).** `stop()` fait `Event.set()` puis `join(timeout)` ; remplacer `time.sleep` par `self._stop.wait(seconds_per_tick)` ; avancer l'horloge **après** `run_tick` sous `threading.Lock` ; verrou dans `MockDataSource` ; passer les seuils explicitement à la boucle plutôt que de partager l'objet `AppConfig`. Critère : test qui enchaîne 200 fois `stop()`/`start()` sans jamais observer deux threads actifs.

**P2-3 — Une transaction par tick, WAL, index (Perf-1, Perf-7).** Ajouter `Repository.save_tick(samples, forecasts, anomalies, incidents)` (une session, un `commit`), remplacer `session.merge` par un `INSERT ... ON CONFLICT DO UPDATE` en masse, activer `PRAGMA journal_mode=WAL` et `synchronous=NORMAL` (à l'ouverture de connexion), ajouter `Index` sur `incidents.status` et `incidents.last_seen_at`. Critère : un tick = un `commit` (test avec compteur d'événements SQLAlchemy) ; `EXPLAIN QUERY PLAN` sans `SCAN`.

**P2-4 — Rétention et croissance (Perf-5, Perf-6).** Purge périodique configurable des mesures/prévisions/anomalies (les incidents sont conservés) ; plafonner `evidence` (N premières + N dernières) ou la normaliser dans sa propre table.

**P2-5 — Schéma évolutif.** Les phases suivantes ajoutent des colonnes (3 champs d'anomalie, features, alertes). Introduire **Alembic** (ou, au minimum, `PRAGMA user_version` + migrations scriptées) avant de modifier le schéma.

**P2-6 — Données perdues (Q-3).** Ajouter à `anomalies` les colonnes `traffic_deviation_percent`, `loss_deviation_points`, `traffic_out_of_interval` et les relire dans `get_anomalies`. Passer les tables en `Mapped[...]` typé (N-12).

**P2-7 — Reprise et calibration (N-8, N-9, N-10).** Arrondir l'horloge simulée à la grille de l'intervalle ; charger l'historique **une seule fois** dans `calibrate()` ; en mode démo, l'écrire en base en masse (historique immédiatement visible sur 1 mois) ; au redémarrage, reprendre depuis le dernier horodatage persisté ou réinitialiser explicitement la base de démonstration.

**P2-8 — Divers sécurité.** Masquer `/docs` et `/openapi.json` hors mode développement (S-5), limitation de débit (S-4), en-tête `Content-Security-Policy`, auto-hébergement des polices (S-8).

### Phase 3 — Qualité de détection, mesurée (≈ 3 j)

**3a — Banc d'évaluation (§33) — à faire en premier (≈ 1,5 j).**

- Créer `network_monitor/evaluation/` : rapprochement `ground_truth()` ↔ incidents détectés (chevauchement temporel), calcul de Precision, Recall, F1, taux de faux positifs par jour et par sortie, délai de détection, délai de fermeture, erreur de durée ; côté prévision : MAE, RMSE, sMAPE, couverture de l'intervalle à 80 %.
- Évaluer sur une période **distincte** de la calibration (exigence §33).
- Commande `python -m network_monitor.evaluation --days 30 --seeds 1..5` produisant un tableau (et un JSON) reproductible.
- Critère : les chiffres de N-3 (491 enregistrements, 5 `OPEN`) sont reproduits par cet outil, puis servent de **référence de départ**.

**3b — Réduire le bruit et fusionner les incidents (N-3) (≈ 1,5 j).**

- Ne pas persister les `SUSPECT` qui retombent sous `warning_score` (ou les stocker comme `dismissed` avec durée de vie courte) ; ne plus créer d'enregistrement à chaque signal faible.
- Ajouter une fenêtre de fusion configurable (`merge_window`, par exemple 2 × `recovery_duration`) : une nouvelle anomalie survenant peu après une fermeture **rouvre le même incident**.
- Passer le backtest en *rolling-origin* (Q-5) et vérifier que le critère ne favorise plus structurellement la baseline.
- Régler les seuils par les mesures de 3a, pas à l'intuition.
- **Objectifs de départ (à ajuster une fois la mesure disponible) :** 0 enregistrement de bruit persisté ; au plus 1 `OPEN` de faux positif par sortie et par 30 jours normaux ; chaque scénario injecté du §34 produit **exactement un** incident ; délai d'ouverture ≤ `minimum_duration` + 1 pas.

### Phase 4 — Fonctionnalités manquantes du cahier des charges (≈ 5–6 j)

| ID | Fonctionnalité | Contenu | Critère de validation |
|---|---|---|---|
| **P4-1** | **Alertes (§27/§28)** | Module `alerts/` avec `AlertManager` et *sinks* interchangeables : journal structuré, webhook HTTP (avec signature), flux SSE `GET /alerts/stream` vers le frontend. Enrichir `Alert` des champs du §27 (type, début, durée, attendu, observé, écart, pertes, raisons) et générer le texte du §28. Anti-spam : une alerte par événement du moteur, jamais une par tick. Brancher `alerts.enabled` et `alerts.warning_score` (ou les supprimer, cf. Q-1). Table `alerts` persistée. | test d'intégration Mock → prévision → détection → incident → **alerte** (§34) ; message d'alerte comparé à l'exemple du §28 |
| **P4-2** | **Features (§9)** | `analysis/features.py` : `traffic_vs_expected`, `loss_vs_expected`, `rolling_std_1h`, `hour`, `day_of_week` ; stockage (colonnes/table `features`) ; `analysis/features_extended.py` désactivé par défaut. | tests unitaires des features (exigés par §34) ; `rolling_std_1h` consommé par la normalisation (§16) |
| **P4-3** | **Recalibration (§32)** | Tâche périodique (`recalibration.interval`, par défaut 24 h) qui refait `fit` hors du thread de monitoring, puis échange atomiquement le modèle ; **exclut** de l'ajustement les plages couvertes par un incident `OPEN`/`STALE`. | test : après un incident long, le modèle recalibré n'a pas « appris » l'anomalie |
| **P4-4** | **Abstraction des détecteurs (§36)** | Interface `AnomalyDetector`, `StatisticalDetector` (code actuel de `detection.py`), `IsolationForestDetector` **optionnel** (n'utilise que `traffic_vs_expected`, `loss_vs_expected`, statistiques glissantes ; **ne décide jamais seul** d'ouvrir un incident, §17 : il confirme/enrichit). `scikit-learn` devient une dépendance optionnelle (`extras`). Câbler `detection.*` ou supprimer les champs. | l'ajout d'un détecteur ne modifie ni `IncidentManager` ni `Repository` (critère explicite du §36) |
| **P4-5** | **Sources réelles** | Adaptateurs `DataSource` : CSV/Parquet (relecture d'un export), SQL (par exemple MariaDB via SQLAlchemy), et un adaptateur SNMP ou Prometheus. Contrat de test commun (`DataSourceContract`) exécuté sur chaque implémentation ; gestion des retards et des sorties dynamiques (`register_output`). | mêmes tests de contrat verts pour Mock et pour au moins un adaptateur réel |
| **P4-6** | **Endpoints manquants** | `GET /outputs/{id}/summary?at=` (agrégats côté serveur, Perf-2), `GET /alerts`, `GET /evaluation/latest`, `GET /incidents/{id}/timeline` (preuves formatées pour l'affichage). | schéma OpenAPI versionné, tests d'API |
| **P4-7** | **Configuration (§35)** | Fichier `config.example.yaml` complet ; surcharge par variables d'environnement ; test de `from_yaml` et de `_parse_duration` (formats `2days`, `10 minutes` refusés avec un message clair : aujourd'hui `float('2day')` fuit en `ValueError`). | tests de chargement + refus des valeurs invalides |

### Phase 5 — Frontend (≈ 4 j)

| ID | Action | Origine | Critère |
|---|---|---|---|
| P5-1 | Base de temps unique (voir P1-1) | N-1 | fait en Phase 1 |
| P5-2 | Boutons de repli : ne garder que le vrai `<button>` (ou `<summary>`), supprimer `role="button"`/`onKeyDown` sur le conteneur (double déclenchement Entrée, Espace sans `preventDefault`) | A11y-1 📖 | test Testing Library : Entrée puis Espace basculent **une** fois |
| P5-3 | Focus visible global, `<h1>`, landmarks (`header`/`main`/`section`), lien d'évitement, `role="img"` + `aria-label` sur le SVG et tableau de données alternatif, `aria-live` sur le journal d'incidents | A11y-2 à A11y-5 | audit `axe` sans violation critique ; navigation clavier complète |
| P5-4 | `AbortController` + délai maximal + backoff sur 429/503 ; `ErrorBoundary` ; indicateur d'âge de la dernière mise à jour | Perf-2, frontend | coupure serveur simulée : message explicite, pas d'empilement de requêtes |
| P5-5 | Décimation des séries longues (LTTB ou min/max par pixel, qui préserve les pics) ; retours de `prev` quand rien ne change dans `ensureOutputDefaults` | Perf-3, Perf-4 | 8 640 points rendus sans à-coups ; 0 re-rendu inutile mesuré |
| P5-6 | Crosshair + infobulle (souris **et** clavier), marges d'axe (`MARGIN.left` ≥ 66), libellé « maintenant » | design | valeur exacte lisible au survol |
| P5-7 | Paliers responsive (≤ 720 px), overflow du tableau sur un conteneur dédié | design | aucun débordement horizontal à 375 px |
| P5-8 | Panneau d'alertes (SSE) et vue détail d'incident (chronologie des preuves, §28 : « compréhensible sans connaître le modèle ») | P4-1, P4-6 | un incident se lit sans consulter le code |
| P5-9 | Types générés depuis `openapi.json` (`openapi-typescript`), passage progressif à TypeScript ; `encodeURIComponent` sur les identifiants | N-13 | `tsc --noEmit` vert |
| P5-10 | Tests : Vitest + Testing Library (composants), Playwright (parcours de bout en bout contre l'API de test) | — | couverture des parcours critiques |

### Phase 6 — Industrialisation (≈ 1 j)

- **CI GitHub Actions** avec matrice **Ubuntu + Windows** (le défaut de verrou de fichier n'apparaît que sous Windows) : `pytest` (avec `--cov` et seuil), `ruff check` + `ruff format --check`, `mypy`, `npm ci && npm run build && npm test`.
- `pre-commit` (ruff, mypy, hooks de base), `.editorconfig`, `CONTRIBUTING.md`, `SECURITY.md` (une fois le jeton en place), `CHANGELOG.md`.
- `Dockerfile` pour l'API et `docker-compose.yml` (API + frontend), utilisateur non root, API liée à `127.0.0.1` par défaut.
- Tâche de fond de l'évaluation §33 en CI (non bloquante au début, puis avec seuils de non-régression sur F1 et FPR).
- Corriger les 41 alertes `ruff` (26 automatiques) et les 39 erreurs `mypy` ; activer `mypy --strict` par paquet, progressivement.

---

## 4. Résumé de l'effort et jalons

| Phase | Contenu | Effort | Jalon |
|---|---|---|---|
| 0 | Installation, tests portables, docs honnêtes | 0,5 j | dépôt installable, CI locale verte |
| 1 | Défauts fonctionnels (horloge, Holt-Winters, trous, types, config, entrées) | 2 j | **le dashboard affiche des données correctes** |
| 3a | Banc d'évaluation §33 | 1,5 j | chiffres de référence reproductibles |
| 2 | Sécurité, concurrence, stockage | 2,5 j | API non pilotable par un tiers, base bornée |
| 3b | Bruit et fusion d'incidents | 1,5 j | objectifs de mesure atteints |
| 4 | Alertes, features, recalibration, détecteurs, sources réelles | 5–6 j | conformité fonctionnelle au cahier des charges |
| 5 | Frontend | 4 j | accessible, réactif, lisible |
| 6 | CI, Docker, documentation | 1 j | publiable |
| **Total** | | **≈ 18–19 j** | |

## 5. Définition de « terminé » (à valider avant publication)

1. `git clone` → installation → démarrage → dashboard avec données visibles, en suivant uniquement le README, sous Windows et Linux.
2. CI verte : tests, `ruff`, `mypy`, build et tests frontend.
3. Aucun endpoint de contrôle sans authentification ; aucun 500 sur entrée invalide.
4. Rapport d'évaluation §33 publié et reproductible, avec les objectifs de la phase 3b atteints.
5. Toutes les fonctionnalités du §2.3 livrées **ou** explicitement retirées de la configuration et du cahier des charges (jamais un champ de configuration sans effet).
6. Aucune affirmation de la documentation contredite par l'état réel du dépôt.

---

## Annexe — Commandes de vérification utilisées

```bash
git clone --depth 1 https://github.com/OrientAbbey/network-monitor.git

# Installation aux dernières versions stables (Python 3.12.3)
python -m venv venv && . venv/bin/activate && pip install -r requirements.txt
# fastapi 0.141.1, starlette 1.7.0, pydantic 2.13.5, SQLAlchemy 2.1.1, numpy 2.5.3,
# statsmodels 0.15.0, uvicorn 0.54.0, httpx 0.28.1, pytest 9.1.1, ruff 0.16.9, mypy 2.3.1

# Tests : 42 (unitaires) + 24 (API + scénarios) = 66 passés, ~42 s
python -m pytest network_monitor/tests -q

# Frontend : échec à froid, puis correctif validé
cd frontend && npm ci                       # ERESOLVE (plugin-react 4.7.0 vs vite 8.3.1)
npm pkg set devDependencies.@vitejs/plugin-react=^6.1.1 dependencies.react=^19.3.0 dependencies.react-dom=^19.3.0
rm -rf node_modules package-lock.json && npm install && npm run build && npm audit --omit=dev
# → build OK, 0 vulnérabilité

# Horloge frontend/backend : serveur réel, comparaison heure réelle / simulée / échantillons visibles
python -m uvicorn network_monitor.app.api:app --port 8765

# Analyse statique
ruff check network_monitor        # 41 alertes (26 corrigeables)
mypy network_monitor --ignore-missing-imports    # 39 erreurs

# Dépendances inutilisées
grep -rn "import polars\|from sklearn\|import scipy" network_monitor    # 0 résultat

# Évaluation rapide (Mock seed 3, calibration 40 j, baseline saisonnière)
#   7 jours normaux -> 491 incidents créés, dont 5 OPEN
#   intermittent 60 min -> 13 enregistrements, 4 OPEN
```
