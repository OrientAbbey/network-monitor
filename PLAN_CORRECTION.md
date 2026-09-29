# Plan de correction et de complément — Network Monitor

**Date :** 29 septembre 2026
**Base :** clone à jour de `master`, `REVUE.md` existant, lecture intégrale du code, exécution réelle (tests, `npm install`/`build`, requêtes API, scripts de reproduction ciblés)
**Objectif de ce document :** (1) vérifier indépendamment `REVUE.md`, (2) documenter ce qu'il ne couvre pas, (3) recenser les fonctionnalités du cahier des charges absentes du code (pas de simples bugs), (4) fournir un plan de correction unique et priorisé.

Ce document **complète** `REVUE.md`, il ne le remplace pas : je n'y répète pas le détail déjà bien exposé (S-1 à S-10, Perf-1 à Perf-7, A11y-1 à A11y-6, etc.), sauf quand une vérification change la conclusion.

---

## Avancement (mis à jour après l'exécution des Lots 0 et 1)

**Fait, testé (98 tests verts, dont 32 nouveaux tests de non-régression — vérifiés en échec sur le code d'origine) et vérifié sous `uvicorn` réel :** items 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17, 21, 24 (partiel : tests des correctifs ci-dessus), 25 (partiel : `dispose()`), plus `pyproject.toml`.
**Non traité :** items 16 (rétention/purge), 18-20, 22-23, Lot 3 (frontend a11y/perf), Lot 4 (fonctionnalités §32/§33/§27).
**Erreurs de ce document corrigées ci-dessous** (deux reprises non vérifiées de REVUE.md, et un diagnostic faux de ma part) : voir « Rectificatifs ».

## Rectificatifs (constatés en exécutant les travaux)

1. **P0-4 (`.gitignore` absent) est faux** : un `.gitignore` complet existe déjà et est versionné. Je l'avais marqué « Confirmé » dans le tableau du §1 sans l'avoir réellement vérifié. Aucune action nécessaire.
2. **`LICENSE` (MIT) existe déjà** ; REVUE.md la déclarait absente. Aucune action nécessaire.
3. **§3.5 — mon diagnostic était incorrect.** Je l'attribuais à l'échec de convergence de statsmodels. La cause réelle : `HoltWintersModel.predict()` appelait `forecast(n_steps)`, qui prévoit toujours depuis la *fin de la série d'entraînement*, sans jamais utiliser `from_time` — donc chaque prévision « 1 pas » renvoyait la même valeur, quelle que soit l'heure (le modèle, lui, varie bien de 173 à 416 Mbps sur 24 h). Corrigé en convertissant chaque horodatage en nombre de pas depuis la fin d'entraînement ; après correctif, la prévision à 0h/6h/12h/18h vaut 193/234/398/356 Mbps (baseline : 194/240/402/361). La non-convergence est réelle mais secondaire : l'item 21 a donc été implémenté en **journalisation + attribut `converged`** plutôt qu'en rejet du modèle (rejeter un ajustement qui donne des prévisions saisonnières plausibles n'aurait pas été justifié).
4. **Conséquence à noter** : sans recalibration périodique (§4.1, non traité), l'écart entre la fin d'entraînement et l'instant prédit ne fait que croître avec Holt-Winters — un argument de plus pour l'item 26.

---

## 1. Vérification indépendante de REVUE.md

| # | Point de REVUE.md | Vérifié comment | Résultat |
|---|---|---|---|
| P0-1 | `npm install`/`npm ci` échouent à froid | `rm -rf node_modules`, `npm install` sur clone neuf (Node 22, npm 10) | **Confirmé.** `ERESOLVE` exact reproduit. Le correctif proposé (`@vitejs/plugin-react` → `^5.0.0`) a été testé isolément : `npm install` réussit, `npm run build` produit un bundle propre (166 Ko, 111 ms). |
| P0-2 | Aucune authentification, CORS `*` | `TestClient` avec `Origin: https://evil.example.com` | **Confirmé.** `POST /monitoring/stop` et `PUT /config` (avec `open_score: 0.99`) réussissent tous les deux en `200`, en-tête `access-control-allow-origin: *` renvoyé. |
| P0-3 | Suite de tests rouge (54/66) | `pytest network_monitor/tests/ -q` sur cette machine (Linux, disque du conteneur) | **À nuancer, voir §2 ci-dessous — important.** |
| P0-4 | Pas de `.gitignore` | `git ls-files`, lecture du fichier | **Infirmé** (rectificatif n°1) : un `.gitignore` complet est déjà versionné. |
| Perf-1 | ~1,5 s/tick, dominé par `fsync` | Non re-benchmarké (dépend du disque de la machine de mesure) | Non contredit ; la cause (plusieurs `commit()` SQLite par tick, 7 à 10 selon le code de `main.py`/`repository.py`) est confirmée par lecture directe du code. Le correctif WAL est standard et sans risque. |
| Q-5 | Backtesting biaisé en faveur de la baseline | Lecture de `_backtest_mae` | **Confirmé**, et ce biais a un effet de bord positif inattendu — voir §3.5. |

## 2. Correction importante à apporter à REVUE.md : la suite de tests n'est pas universellement rouge

Sur cette machine (Linux, disque du conteneur), **les 66 tests passent, en 23,6 secondes** :

```
66 passed, 1 warning in 23.60s
```

Cela ne veut pas dire que `REVUE.md` a tort sur le fond — ça veut dire que les deux causes qu'il identifie sont **dépendantes de la plateforme et de la vitesse du disque**, pas des bugs qui se manifestent partout :

- **Cause 1 (descripteur SQLite non libéré)** est un comportement Windows : sous Linux, un fichier ouvert peut être supprimé (`os.remove`) sans erreur même si un pool de connexions le tient encore ouvert. Le défaut (`dispose()` manquant) **existe bel et bien** dans le code — mais son symptôme (`PermissionError` au teardown) ne se produit que sous Windows.
- **Cause 2 (`time.sleep(2.5)` insuffisant)** est un symptôme direct de Perf-1 : sur un disque où `commit()` prend ~166 ms (mesuré par REVUE.md sur la machine de test Windows), un tick dure ~1,5 s et 2,5 s ne permettent qu'un seul tick. Sur ce conteneur, les ticks sont nettement plus rapides (pas de `fsync` lent), donc 2,5 s en permettent plusieurs et le test scripté passe.

**Conséquence pour la priorisation :** le correctif structurel (`dispose()` + fixtures propres, remplacement des `time.sleep()` par une attente active) reste tout aussi nécessaire — un test qui ne passe que "si le disque est assez rapide" est un test cassé, même s'il passe ici. Mais la formulation à corriger dans le futur rapport de suivi n'est pas "54/66 passent", c'est "la suite contient une dépendance cachée et non testée à la vitesse du disque hôte, qui la rend non déterministe **par construction**, y compris sur une machine où elle passe aujourd'hui".

---

## 3. Constats supplémentaires — absents de REVUE.md

Six défauts fonctionnels indépendants du precedent rapport, tous reproduits avec un script isolé.

### 3.1 🔴 CRITIQUE — Un trou de données fantôme est généré à **chaque tick**, sur **chaque sortie**

**Fichiers :** `network_monitor/app/main.py:85`, `network_monitor/data/source.py:129-149`

C'est le constat le plus sérieux de cette passe : le mécanisme censé détecter les trous de données (§31 du cahier des charges) génère en réalité un faux trou (`DataQualityFlag.MISSING`) à **chaque pas de la boucle, pour chaque sortie**, même quand tout fonctionne normalement. Reproduction isolée (aucun scénario injecté, fonctionnement 100 % normal) :

```
tick 1: now=00:05  échantillons réels reçus=3 (ts=['00:00'])  slots MISSING générés=3  <-- PHANTOME
tick 2: now=00:10  échantillons réels reçus=3 (ts=['00:05'])  slots MISSING générés=3  <-- PHANTOME
tick 3: now=00:15  échantillons réels reçus=3 (ts=['00:10'])  slots MISSING générés=3  <-- PHANTOME
...
```

**Cause.** `MonitoringLoop.run_tick(now)` appelle :

```python
# app/main.py:83-85
validated = self._validator.validate_batch(raw, now=now)
for output_id in self._source.list_outputs():
    validated += self._validator.missing_slots(output_id, now)   # <- borné sur `now`
```

Or l'échantillon qui vient d'arriver dans `raw` porte le timestamp `now - interval` (c'est la sémantique de `MockDataSource.get_latest()` : il renvoie la fenêtre `[horloge_avant, horloge_avant + interval)`, et `now` est déjà avancé d'un cran par l'appelant — `api.py:99` ou `main.py:154` — *avant* le tick). `validate_batch` met donc à jour `_last_valid_timestamp[output] = now - interval`. `missing_slots(output_id, now)` cherche alors tout créneau attendu entre ce dernier timestamp valide (exclu) et `now` (inclus) : il en trouve exactement un — `now` lui-même — qui n'est simplement pas encore arrivé, pas réellement manquant.

**Impact réel.**
- Sans incident actif : inoffensif (retour anticipé dans `IncidentManager._handle_data_gap`), mais consomme un appel `process()` inutile par sortie et par tick.
- **Avec un incident actif (SUSPECT/OPEN/STALE) : chaque tick ajoute une fausse preuve `data_gap=True` à `incident.evidence`, en plus de la vraie.** Vérifié sur un scénario scripté de 40 minutes : **45 % des preuves stockées pour l'incident (14 sur 31) sont des trous fantômes.** Cela double quasiment le volume d'écriture d'`evidence` et aggrave directement Perf-6 de REVUE.md (croissance O(n²) du blob JSON réécrit intégralement à chaque tick).
- Le mécanisme de détection de trous réels (§31) est donc **actuellement invérifiable en pratique** : un opérateur qui consulte l'`evidence` d'un incident ne peut pas distinguer un vrai trou de collecte d'un artefact systématique. C'est cohérent avec le constat de REVUE.md (§7) : `missing_slots` n'a **aucun test dédié** — c'est précisément ce vide de couverture qui a laissé passer ce bug depuis le premier commit.

**Correctif vérifié.** Il ne suffit pas de changer la borne de `now` à `now - interval` en gardant l'ordre d'appel actuel : `validate_batch` a déjà avancé `_last_valid_timestamp` avant que `missing_slots` ne soit appelé, ce qui **masque les vrais trous** au lieu de les détecter (vérifié : avec cette version naïve, une panne réelle de 2 pas n'est plus du tout détectée). Le correctif qui fonctionne dans les deux cas — testé sur fonctionnement normal (0 faux trou) **et** sur une panne simulée de 2 pas (les 2 trous bien détectés, sur les 3 sorties, une seule fois, sans re-signalement au tick suivant) — inverse l'ordre et corrige la borne :

```python
# app/main.py — run_tick(), avant l'appel à validate_batch
missing = []
for output_id in self._source.list_outputs():
    missing += self._validator.missing_slots(output_id, now - 2 * self._source.get_interval())
validated = missing + self._validator.validate_batch(raw, now=now)
```

L'idée : détecter les trous **avant** de laisser le paquet du tick courant mettre à jour `_last_valid_timestamp`, et borner sur `now - 2×interval` (l'horizon du dernier créneau qui aurait dû être rempli *avant* celui qui arrive ce tick-ci), pas sur `now` ni sur `now - interval`.

**Priorité : P0.** Ce n'est pas un défaut cosmétique — c'est le mécanisme central du §31 qui ne fait actuellement pas ce qu'il prétend faire, silencieusement, depuis le premier commit.

### 3.2 🟠 La validation des seuils n'empêche pas `warning_score` hors de l'ordre attendu

**Fichier :** `network_monitor/app/config.py:81-87`, commentaire trompeur dans `network_monitor/app/api.py:301-303`

Le commentaire du code (et `REVUE.md` qui le cite en exemple de bonne pratique — §6, "Validation des seuils par reconstruction") affirme que `PUT /config` applique la règle **§21 : `close_score < warning_score < open_score`**. En réalité, le validateur Pydantic ne vérifie que `close_score < open_score` :

```python
@field_validator("close_score")
@classmethod
def _close_below_open(cls, v, info):
    open_score = info.data.get("open_score")
    if open_score is not None and v >= open_score:
        raise ValueError("close_score doit être strictement inférieur à open_score (§21)")
    return v
```

Reproduit :

```
IncidentConfig(open_score=0.75, close_score=0.40, warning_score=0.90)  -> ACCEPTÉ
```

**Conséquence fonctionnelle**, pas seulement documentaire : avec `warning_score=0.90 > open_score=0.75`, toute la plage de score entre `0.75` et `0.90` devient une **zone morte** — un score qui y entre ne déclenche ni l'entrée en `SUSPECT` (qui exige `score >= warning_score = 0.90`) ni, une fois `SUSPECT` atteint, une lecture cohérente de la hiérarchie des seuils. Un opérateur qui règle ces trois valeurs via `PUT /config` (l'interface le permet, aucune erreur 422) obtient un moteur d'incidents partiellement aveugle, sans le moindre avertissement.

**Correctif :** ajouter un `model_validator(mode="after")` sur `IncidentConfig` qui vérifie `close_score < warning_score < open_score` dans son ensemble, pas seulement la paire `close`/`open`.

### 3.3 🟡 Durées de configuration négatives ou nulles acceptées silencieusement

**Fichier :** `network_monitor/app/config.py:15-24` (`_parse_duration`)

```
_parse_duration('-5m')  -> timedelta(-1, 86100)   # soit -5 minutes, ACCEPTÉ
_parse_duration('0')    -> ValueError               # échoue, alors que _parse_duration(0) (int) réussit
_parse_duration('1.5d') -> timedelta(1, 43200)      # accepté (pas forcément voulu, mais raisonnable)
```

Aucun champ `timedelta` de `IncidentConfig` (`minimum_duration`, `recovery_duration`, `stale_after`) n'a de contrainte de positivité. Une `minimum_duration` négative rendrait `elapsed >= min_duration` (dans `IncidentManager._process_active_incident`) vrai dès le premier tick au-dessus de `open_score`, contournant complètement l'hystérésis du §21. `"0"` (chaîne) échoue alors que `0` (nombre) est accepté — incohérence mineure mais qui peut surprendre dans un fichier YAML où les deux formes sont plausibles.

**Correctif :** ajouter `if number <= 0: raise ValueError(...)` dans `_parse_duration` (sauf si une durée nulle a un sens métier explicite — la lecture du cahier des charges n'en trouve aucun), et gérer le cas `"0"` sans suffixe.

### 3.4 🟡 Un doublon de mesure est traité en interne comme un trou de données

**Fichier :** `network_monitor/data/source.py` (drapeau `DUPLICATE` inclus dans `_BLOCKING_FLAGS` via `is_usable`), `network_monitor/app/main.py:88-100`

`ValidatedSample.is_usable` est `False` dès qu'un `DataQualityFlag.DUPLICATE` est posé (`sample=None` dans ce cas). Or `_process_validated_sample` ne distingue pas *pourquoi* `is_usable` est faux : un doublon suit exactement le même chemin qu'un vrai trou — `AnomalyResult(data_gap=True, ...)`. Vérifié :

```
Doublon -> is_usable = False | flags = ['duplicate'] | sample = None
  => main.py le traite comme data_gap=True
```

Ce n'est pas anodin en combinaison avec 3.1 : une source réelle qui redélivre occasionnellement le même point (retransmission réseau, par exemple) apparaîtrait dans l'`evidence` d'un incident comme une "absence de données", ce qui est trompeur pour un opérateur qui essaie de comprendre ce qui s'est passé.

**Correctif :** distinguer, dans `_process_validated_sample`, un doublon (donnée présente mais rejetée) d'un vrai trou (rien reçu) — au minimum ne pas les faire passer tous les deux par la branche `data_gap=True`.

### 3.5 🟠 Holt-Winters produit une prévision plate (aucune saisonnalité) — ⚠️ cause réelle différente de celle décrite ci-dessous, voir Rectificatif n°3

**Fichier :** `network_monitor/analysis/forecasting.py` (`HoltWintersModel.fit`/`select_model`)

Avec le **seed par défaut de production** (`MockDataSource()`, seed=42) et un historique de 50 jours, `statsmodels` échoue à converger (`ConvergenceWarning`) sur les 3 sorties, et le modèle ajusté produit une prévision **rigoureusement constante** quelle que soit l'heure de la journée demandée :

```
OUT_001: prévisions à 0h/6h/12h/18h = [192.8, 192.8, 192.8, 192.8]  (écart=0.0 Mbps)
OUT_002: prévisions à 0h/6h/12h/18h = [2169.8, 2169.8, 2169.8, 2169.8]  (écart=0.0 Mbps)
OUT_003: prévisions à 0h/6h/12h/18h = [26241.2, 26241.2, 26241.2, 26241.2]  (écart=0.0 Mbps)
```

C'est l'inverse exact de ce que Holt-Winters est censé apporter (§10.B : porter la saisonnalité quotidienne). `select_model` ne vérifie ni le statut de convergence renvoyé par `statsmodels` (`fit_result.mle_retvals`), ni que le modèle produit une variation raisonnable — seul le MAE de backtest compte.

**Pourquoi ce n'est pas (encore) visible en usage réel :** `DEFAULT_API_CONFIG` (`api.py:33`) fixe volontairement `history.preferred = 40 jours`, **sous** le seuil `model_selection.minimum_history = 45 jours` par défaut — `select_model` retombe alors systématiquement sur la Seasonal Baseline sans jamais essayer Holt-Winters. C'est un choix documenté et assumé (commentaire `api.py:29-36`), pas un hasard. Mais `docs/ARCHITECTURE.md` recommande explicitement de passer `AppConfig.defaults()` (90 jours) pour un déploiement réel — c'est-à-dire **au-dessus** du seuil de 45 jours, le chemin où Holt-Winters redevient candidat. Avec un historique de 90 jours et le seed de production, le test effectué ici a vu le biais de `_backtest_mae` (Q-5 de REVUE.md) favoriser malgré tout la baseline — mais rien ne garantit que ce soit toujours le cas pour un autre historique ou une autre sortie : le seul garde-fou actuel contre un modèle cassé est un **biais méthodologique non intentionnel**, pas une vérification explicite.

**Correctif :** dans `HoltWintersModel.fit`, inspecter `self._fit_result.mle_retvals` (ou l'attribut équivalent selon la version de `statsmodels`) et lever une exception si l'optimisation n'a pas convergé — `select_model` disqualifiera alors proprement le candidat au lieu de risquer de le retenir un jour avec un jeu de données différent.

### 3.6 🟡 Le score affiché sur un incident peut contredire sa sévérité affichée

**Fichier :** `network_monitor/incidents/manager.py:159` (`incident.score = max(...)`) vs `:179`/`:191` (`incident.severity = self._severity_for(score)`)

`incident.score` est un **maximum glissant** (ne redescend jamais tant que l'incident est actif), alors que `incident.severity` est **recalculée à chaque tick à partir du score instantané**. Reproduit :

```
Sévérité à score .90 : critical -> puis à .76 : warning (rétrogradation pendant l'incident)
```

Résultat concret dans `_describe()` (utilisé pour le message d'alerte et potentiellement l'affichage) : un incident peut afficher simultanément `score=0.90` (le pic historique) et `sévérité=warning` (l'état courant, redescendu sous `critical_score`). Ce n'est pas forcément un bug de logique — la sévérité *doit* pouvoir redescendre — mais l'incohérence d'affichage entre un champ "pic" et un champ "instantané" sans que l'un ou l'autre ne soit clairement étiqueté comme tel peut légitimement passer pour un bug aux yeux d'un opérateur.

**Correctif suggéré :** soit exposer les deux notions séparément (`score` instantané *et* `peak_score`), soit documenter explicitement dans le modèle `Incident` que `score` est un maximum et que `severity` ne l'est pas.

---

## 4. Fonctionnalités du cahier des charges absentes du code

Ces trois points sont différents des sections précédentes : il ne s'agit pas de bugs dans du code existant, mais d'exigences explicites du cahier des charges (`docs/CAHIER_DES_CHARGES_RESEAU.md`) pour lesquelles **aucun code n'existe**, à quelque endroit du dépôt que ce soit (vérifié par recherche exhaustive de mots-clés).

### 4.1 §32 — Aucune recalibration périodique (dérive du modèle)

> « Le système doit donc permettre une recalibration périodique. […] La fréquence doit être configurable. »

`MonitoringLoop.calibrate()` n'est appelée **qu'une seule fois**, au démarrage (`MonitoringState.calibrate()` dans le `lifespan` FastAPI). Aucun champ de configuration ne porte une fréquence de recalibration (`AppConfig` n'a rien de tel), aucun mécanisme ne la déclenche. Un déploiement qui tourne plusieurs semaines utilisera indéfiniment le modèle calibré au premier démarrage, même si le comportement réel de la sortie a dérivé — exactement le scénario que le §32 demande d'anticiper.

**Ce qu'il faut ajouter :**
- `MonitoringConfig.recalibration_interval: timedelta | None` (ex. `24h` par défaut, `None` pour désactiver).
- Dans la boucle (`_run` de l'API ou `run_forever`), déclencher `loop.calibrate(now)` quand l'intervalle est écoulé — en excluant les périodes fortement anormales de la fenêtre de recalibration, comme l'exige explicitement le §32 (« Les périodes identifiées comme fortement anormales ne doivent pas être aveuglément utilisées pour recalibrer le comportement normal ») : concrètement, exclure de l'historique de recalibration les plages couvertes par un incident `OPEN`/`STALE` connu.

### 4.2 §33 — Aucun module d'évaluation quantitative

> « Le Mock doit permettre une évaluation quantitative. […] Precision, Recall, F1, False Positive Rate, Detection Delay, Closure Delay, Duration Error […] MAE, RMSE, sMAPE, Prediction Interval Coverage. »

Recherche exhaustive dans `network_monitor/` : **aucune occurrence** de ces métriques, ni de code qui rapproche `MockDataSource.ground_truth()` (qui existe et est déjà exploitable — c'est la vérité terrain des scénarios injectés) des incidents réellement détectés ou des prévisions réellement produites. Le Mock a été construit avec cette évaluation en tête (`ground_truth()` existe justement dans ce but, avec un commentaire qui le dit explicitement), mais rien en aval ne l'utilise. C'est une fonctionnalité complète du §33, pas un ajustement mineur.

**Ce qu'il faut ajouter :** un module `network_monitor/evaluation/` avec :
- Appariement `AnomalyScenario` (vérité terrain) ↔ `Incident` détectés, par recouvrement temporel sur la même `output_id`.
- Calcul de Precision/Recall/F1/FPR au niveau scénario, et Detection Delay (`opened_at - true_start`) / Closure Delay (`closed_at - true_end`) / Duration Error au niveau incident.
- Calcul de MAE/RMSE/sMAPE/PICP en comparant `ForecastResult` stockés à la donnée réellement observée sur la même période.
- Un point d'entrée simple (script ou endpoint `GET /evaluation` en lecture seule) qui exécute cette évaluation **sur une période distincte de celle utilisée pour la calibration**, comme l'exige explicitement le §33.

### 4.3 §27 — Les alertes générées ne sont exposées nulle part (fonctionnalité orpheline, pas manquante — mais inutilisable en l'état)

Ce point nuance Q-2 de `REVUE.md` (« code mort », recommandation : implémenter le transport ou supprimer). Le tableau de synthèse du cahier des charges (§38) classe explicitement les « canaux externes » (webhook, etc.) comme une **alternative future**, pas une exigence V1 — implémenter un transport complet (webhook/SSE) irait donc au-delà du périmètre demandé. Mais l'exigence V1 elle-même — qu'une alerte soit **générée et liée au cycle de vie d'un incident** (§27 : SUSPECT → OUVERT → MISE À JOUR → RÉCUPÉRATION → FERMÉ, avec le contenu détaillé au §27/§28) — **est déjà implémentée correctement** dans `IncidentManager` (la dataclass `Alert`, la méthode `_alert()`), mais sa sortie est jetée par les deux seuls appelants (`main.py:111`, `main.py:134` : `_, incident = self._incident_manager.process(...)`).

**Ce qu'il faut ajouter, à effort minimal et dans le périmètre V1 (pas de canal externe) :** persister les `Alert` générées (une table simple, ou les exposer via un endpoint `GET /outputs/{id}/alerts` construit à partir de l'historique déjà présent dans `Incident.evidence` + les transitions de statut). Actuellement, un client de l'API n'a **aucun moyen** de reconstituer la chronologie SUSPECT→OUVERT→MISES À JOUR→RÉCUPÉRATION→FERMÉ d'un incident — seul l'état courant et la liste d'`evidence` brute (scores, pas d'événements) sont exposés.

---

## 5. Plan de correction complet et priorisé

Reprend les lots de `REVUE.md` (numérotation conservée) et ajoute les nouveaux constats à l'endroit qui correspond à leur gravité, plus un lot 4 pour les fonctionnalités manquantes du §4.

### Lot 0 — avant toute publication (bloquant)

| # | Action | Fichier(s) | Effort |
|---|---|---|---|
| 1 | Aligner `@vitejs/plugin-react` sur Vite 8 (`^5.0.0`), régénérer le lockfile | `frontend/package.json` | 5 min |
| 2 | **[nouveau, §3.1]** Corriger l'ordre d'appel `missing_slots`/`validate_batch` et la borne temporelle — élimine le trou de données fantôme à chaque tick | `app/main.py` | 20 min |
| 3 | ~~`.gitignore` complet~~ — déjà présent (rectificatif n°1) | — | — |
| 4 | ~~`LICENSE`~~ déjà présente ; `pyproject.toml` ajouté (`requires-python >=3.11`, **non vérifié : seul Python 3.12 testé**) | racine | 10 min |
| 5 | `dispose()` sur le moteur SQLAlchemy + appel dans les fixtures | `repository.py`, fixtures de tests | 15 min |
| 6 | Remplacer les `time.sleep()` de test par une attente active sur l'état attendu (rend la suite déterministe indépendamment de la vitesse du disque — cf. §2) | `test_api.py` | 30 min |
| 7 | `PRAGMA journal_mode=WAL` + `synchronous=NORMAL` | `repository.py.__init__` | 10 min |
| 8 | Corriger README/ARCHITECTURE.md (retirer les affirmations de vérification obsolètes) | docs | 15 min |

**Effort total : ~1h50.**

### Lot 1 — sécurité, performance, fiabilité

| # | Action | Fichier(s) | Effort |
|---|---|---|---|
| 9 | CORS sur liste blanche + méthodes restreintes | `api.py:124` | 20 min |
| 10 | Bornes sur les fenêtres temporelles (`max_span`) + rejet des datetimes naïfs | `api.py`, `repository.py` | 30 min |
| 11 | **[nouveau, §3.2]** `model_validator` complet sur `IncidentConfig` : `close_score < warning_score < open_score`, pas seulement `close < open` | `config.py` | 15 min |
| 12 | **[nouveau, §3.3]** Rejeter les durées ≤ 0 dans `_parse_duration`, gérer `"0"` sans suffixe | `config.py` | 15 min |
| 13 | Une seule transaction par tick | `main.py`, `repository.py` | 30 min |
| 14 | Index sur `incidents.status` et `incidents.last_seen_at` | `repository.py` | 5 min |
| 15 | `stop()` → `join()` ; verrou sur `MockDataSource` | `api.py`, `mock.py` | 20 min |
| 16 | Purge périodique des mesures (rétention configurable) | `repository.py` | 45 min |
| 17 | **[nouveau, §3.4]** Ne plus traiter un doublon (`DUPLICATE`) comme un trou de données (`data_gap`) dans `_process_validated_sample` | `main.py` | 20 min |

**Effort total : ~3h20.**

### Lot 2 — qualité et détection

| # | Action | Effort |
|---|---|---|
| 18 | `ruff` + `mypy` en CI | 1 h |
| 19 | Supprimer la configuration inopérante ou la câbler (`detection.traffic`, `isolation_forest`, `forecast.models`…) | 1 h |
| 20 | Persister `traffic_deviation_percent`/`traffic_out_of_interval`/`loss_deviation_points` | 30 min |
| 21 | **[§3.5, révisé]** Corriger `HoltWintersModel.predict()` pour respecter `from_time` (cause réelle) ; journaliser la non-convergence sans rejeter le modèle | `forecasting.py` | 45 min |
| 22 | Backtest par glissement (rolling-origin) au lieu d'un horizon multi-pas d'un coup (Q-5) | `forecasting.py` | 1 h30 |
| 23 | **[nouveau, §3.6]** Séparer `score` (instantané) de `peak_score` sur `Incident`, ou documenter explicitement la sémantique actuelle | `models.py`, `manager.py` | 30 min |
| 24 | Tests manquants : `DataValidator` (doublons, sortie inconnue, retard), `missing_slots` **avec assertion explicite sur l'absence de faux positifs en fonctionnement normal** (aurait détecté 3.1), `from_yaml`, `_parse_duration` | 2h30 |
| 25 | `dispose()` + `delete()` sur `Repository` pour des tests isolables | 20 min |

**Effort total : ~8h05.**

### Lot 3 — frontend (accessibilité, perf, robustesse)

Reprend intégralement les points 20 à 30 de `REVUE.md` §10 (double déclenchement clavier, focus visible, landmarks, `aria-label` sur le SVG, `aria-live`, endpoint d'agrégat serveur, sous-échantillonnage, `ErrorBoundary`/`AbortController`, responsive, crosshair, types générés). Rien à ajouter de nouveau sur ce périmètre — la revue frontend de `REVUE.md` est complète.

**Effort total : ~7h.**

### Lot 4 — fonctionnalités du cahier des charges absentes (nouveau)

| # | Action | Fichier(s) | Effort |
|---|---|---|---|
| 26 | **[§4.1]** Recalibration périodique configurable, en excluant les fenêtres couvertes par un incident actif de l'historique de recalibration | `config.py`, `main.py` (ou `api.py._run`) | 2h |
| 27 | **[§4.2]** Module d'évaluation quantitative (Precision/Recall/F1/FPR/Detection Delay/Closure Delay/Duration Error pour les incidents ; MAE/RMSE/sMAPE/PICP pour le forecasting), appliqué sur une période distincte de la calibration | nouveau module `network_monitor/evaluation/` | 4h |
| 28 | **[§4.3]** Persister ou exposer les `Alert` générées (a minima `GET /outputs/{id}/alerts`), sans aller jusqu'au transport externe (hors périmètre V1 selon §38) | `manager.py`, `repository.py`, `api.py` | 2h |

**Effort total : ~8h.**

---

## 6. Synthèse et ordre de traitement recommandé

**Effort total du plan complet : ~28h**, contre ~13h estimées par `REVUE.md` seul (Lots 0-3) — l'écart vient presque entièrement du Lot 4 (fonctionnalités manquantes, ~8h) et des nouveaux correctifs du Lot 1/2 (§3.1 à 3.6, ~2h40 cumulées).

Ordre recommandé, par dépendance et par risque :

1. **Lot 0**, sans exception — dépôt actuellement non installable à froid et documentation qui affirme des vérifications fausses. Le point 2 (trou de données fantôme) y a été ajouté en priorité P0 : ce n'est pas qu'un défaut de perf ou de style, c'est le mécanisme central du §31 qui ne fonctionne pas comme documenté, et il pollue silencieusement toutes les données déjà collectées depuis le premier commit.
2. **Lot 1**, avant toute exposition au-delà de `localhost` — l'absence d'authentification combinée à CORS `*` permet un contrôle total à distance (confirmé indépendamment, §1).
3. **Lot 4** peut commencer en parallèle du Lot 2 : ce sont des ajouts (aucun risque de régression sur l'existant), alors que le Lot 2 touche à la logique de détection déjà en production.
4. **Lot 2 et Lot 3** en dernier, une fois la base stabilisée — ce sont des lots de qualité et d'expérience, pas de correctifs bloquants.

Le jugement de fond de `REVUE.md` reste valide et je le partage : **le projet est solide sur la conception et faible sur l'industrialisation.** Cette passe ajoute un axe supplémentaire à ce constat — un axe que la seule lecture de code, aussi attentive soit-elle, a du mal à révéler sans exécution ciblée : **le mécanisme central de détection de trous de données (§31) ne fonctionne pas comme documenté depuis le tout premier commit**, masqué précisément par l'absence de test dédié que `REVUE.md` avait déjà signalée comme un vide de couverture (§7). C'est un bon rappel que « zéro test sur X » et « X est cassé » sont souvent la même observation vue sous deux angles différents.
