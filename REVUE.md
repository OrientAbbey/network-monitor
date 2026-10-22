# Revue du projet — état après correctifs

Commit de départ : `6b883b6`. Mesures reproductibles sous Linux / Python 3.12 (source Mock ;
protocole détaillé dans « Mesures »). Évaluation : `python -m network_monitor.analysis.evaluation`.

## Corrigé

| Problème | Correctif | Vérification |
|---|---|---|
| `npm ci` échouait (plugin-react 4 vs Vite 8) | `@vitejs/plugin-react ^6.1.1`, React 19.3, lockfile régénéré | `npm ci && npm run build`, 0 vulnérabilité |
| **Dashboard quasi vide** : le frontend fabriquait ses fenêtres avec l'heure du navigateur, l'API tourne sur une horloge simulée | fenêtres `span_hours` relatives à l'horloge serveur ; `server_time` exposé ; horloge alignée sur la grille de 5 min | serveur réel : 15 échantillons visibles au lieu de 1 |
| `HoltWintersModel.predict()` ignorait `from_time` (prévision figée) | décalage en pas depuis la fin de l'ajustement | prévision à +6 h ≠ prévision à +0 h |
| `missing_slots` ré-émettait tous les trous à chaque tick (5 trous → 15 événements) | curseur du dernier créneau émis | test de régression |
| Bruit d'incidents : 491 lignes en 7 jours de trafic normal | un signal jamais confirmé n'est plus persisté ; fenêtre de fusion `merge_window` ; plafond de 200 preuves | 491 → 4 lignes (les 4 sont des faux positifs confirmés) ; chaque scénario injecté = 1 enregistrement (avant : 3-4) |
| Alerte émise à chaque tick tant qu'un incident est ouvert | `updated` seulement si la sévérité change | test |
| Alertes calculées puis jetées | persistées (table `alerts`, champs du §27), `GET /alerts` | test API |
| Cas « trafic ↑ + pertes ↑ » classé en chute | type `congestion` | test |
| `warning_score` non comparé à `open_score`/`close_score` | validation `close < warning < open` (aussi via `PUT /config`) | 422 |
| `PUT /config` muait la constante partagée `DEFAULT_API_CONFIG` | copie profonde par instance | test |
| Aucune authentification ; CORS `*` | CORS restreint (`NM_CORS_ORIGINS`), `/docs` masqué hors `development` ; endpoints de contrôle d'abord protégés par `NM_API_TOKEN` (503 s'il est absent), puis par `POST /auth/login` — voir la ligne suivante | 401/422 vérifiés sur un serveur réel |
| `since` naïf → HTTP 500 ; plage non bornée | 422 si datetime naïf ; plage max 31 jours | tests |
| Champs de config sans effet (`detection.traffic`, `isolation_forest`, `extended_features`, `forecast.horizon`, `alerts.warning_score`) | supprimés ; `loss.enabled`, `forecast.models`, `history.minimum` câblés | grep : plus aucun champ orphelin |
| 3 champs d'anomalie perdus à l'écriture ; pas d'index ; pas de purge ; pas de `dispose()` | colonnes ajoutées, index `status`/`last_seen_at`, WAL, `purge_older_than`, `dispose()` | tests de repository |
| Recalibration (§32) absente ; historique rechargé N fois à la calibration | recalibration périodique excluant les incidents actifs ; un seul chargement | — |
| Bug clavier : Entrée/Espace sur le bouton interne déclenchait 2 bascules | garde `e.target === e.currentTarget` | lecture du code (non testé en navigateur) |
| Tests dépendants de `sleep`, de `mktemp`, connexions non fermées | attentes actives, `tmp_path`, `dispose()` | suite 44 s → 19 s |
| Dépendances inutilisées (polars, scipy, scikit-learn) | retirées ; polars revenue plus tard pour le CSV/Parquet et scikit-learn en option pour Isolation Forest (lignes suivantes) | 0 import |
| Features §9 absentes | `analysis/features.py` (V1) et `features_extended.py` (§9.2, activable) ; V1 stockées avec chaque anomalie | `tests/test_features.py` |
| Aucun détecteur de confirmation (§17) | `IsolationForestDetector` optionnel : enrichit les preuves, ne décide jamais (mêmes incidents avec et sans) | test comparant deux exécutions |
| Une seule source (Mock) | `CsvDataSource` (rejeu) et `SqlDataSource` (table en lecture seule, identifiants validés, URL jamais renvoyée en clair) choisies par `source.type` | contrat commun testé sur les 3 sources |
| 5 commits par tick | `Repository.save_tick` : une transaction, un commit | test qui compte les commits |
| 39 erreurs mypy, 12 alertes ruff | tables SQLAlchemy en `Mapped[...]`, `Annotated` pour FastAPI | `ruff` sans erreur ; `mypy` : 0 erreur de code, mais 3 `import-untyped` — les stubs tiers de `yaml`, `statsmodels` et `scikit-learn` ne sont pas installés (`types-PyYAML` faltait) |
| Frontend : décimation, infobulle, repères, tests, TypeScript | décimation min/max (pics conservés), graphe lisible à la souris et au clavier, `header`/`main`/`section`, lien d'évitement, zones `aria-live`, panneau d'alertes ; `api.ts`/`types.ts`/`chart.ts` typés ; Vitest + Testing Library | `npm run typecheck && npm test` (36 tests) |
| ~10 faux incidents ouverts par jeu de 3 graines (7 j normaux) | `minimum_duration` 10 → 15 min, `recovery_duration` 10 → 5 min, `min_suspect_steps` = 3, choisis par balayage | tableau « Mesures » : 51 → 7 faux incidents sur 162 sorties-jours |
| Types `progressive_degradation` / `intermittent_degradation` jamais produits | motif temporel sur la magnitude de l'écart (alternance avec hystérésis, rampe corrélée), réévalué à chaque pas et figé pendant le retour à la normale ; un SUSPECT tolère les creux après `min_suspect_steps` pas actifs, pour qu'un intermittent reste un seul incident | exactitude des types 84/87 (96,6 %) sur 96 scénarios ; `tests/test_incident_patterns.py` |
| Pertes *inférieures* à l'habitude comptées comme anomalie (une sortie stable écrêtée à 0 % ouvrait un incident sévère) | seule une hausse des pertes est une anomalie (§25) | test |
| Seuils jamais balayés | banc `capture` / `replay` / `sweep` (rejeu exact des anomalies à travers le moteur d'incidents), jeu de scénarios standard et variantes | tableau ci-dessous |
| Isolation Forest qui ne séparait pas les paliers | deux forêts (niveau, dynamique), score = maximum, calibré sur l'entraînement, garde d'extrapolation | palier soutenu à 1,00 contre 0,05 en moyenne en normal |
| Source SQL non éprouvée sur MariaDB | testée sur MariaDB 10.11 réel (contrat, boucle de bout en bout, connexion coupée) ; `pool_pre_ping` ajouté (le test échoue sans) | `NM_TEST_MARIADB_URL` |
| Jeton en mémoire, pas d'authentification d'utilisateur | `POST /auth/login` : mots de passe scrypt (`NM_USERS`), jeton de session signé HMAC à expiration, 5 échecs / 5 min bloquent la connexion, identité journalisée ; jeton machine conservé | `tests/test_auth.py` (expiration, falsification, limitation) |
| `Dashboard.jsx` hors du typage ; infobulle à la souris non testée | `Dashboard.tsx` en TypeScript strict (`allowJs` retiré) ; calcul du pointeur extrait (`pointerToTime`) et testé, géométrie du SVG simulée | `npm run typecheck`, 36 tests Vitest |

| Dashboard : ~66 Ko par cycle de rafraîchissement, aucune compression | gzip (≥ 500 o), ETag + `Cache-Control: no-cache` (304 sans corps), rafraîchissement **incrémental** des graphes (`since` = dernier point connu, fusion par horodatage), polling suspendu quand l'onglet est masqué | 66,0 → 1,7 Ko par cycle (38×), voir « Mesures » |
| Plusieurs workers = plusieurs boucles sur la même base | verrou de fichier du noyau (`app/lock.py`) : refus de démarrer, y compris sur `--workers 2` (uvicorn s'arrête) et `WEB_CONCURRENCY > 1` ; libéré même après un `kill -9` | `tests/test_http_and_lock.py` (dont deux processus) |
| Calibration : tout l'historique de toutes les sorties en mémoire | `load_history(start, end, output_id)` : une sortie à la fois, libérée avant la suivante ; une sortie non calibrée au démarrage est réessayée chaque heure (et non chaque jour) | pic mémoire 405 → 227 Mo (30 sorties), 839 → 275 Mo (90) |
| Alertes stockées mais jamais envoyées | file d'envoi transactionnelle (écrite avec l'alerte), regroupement (`batch_window`), plafonds de messages, nouvelles tentatives à délai croissant, **Telegram** (groupe), **webhook** signé HMAC-SHA256, **SMS de groupe** (passerelle Android, API Orange) ; secrets dans l'environnement ; `POST /notifications/test/{canal}` | `tests/test_notify.py` (20 tests, dont un vrai serveur HTTP et un incident de bout en bout) |
| Début d'incident = date de confirmation | `started_at` (premier signal faible) et `ended_at` (premier pas sous `close_score`), rétro-datés ; un bruit juste après la fin d'un incident ne le prolonge plus (la fusion attend la confirmation du nouvel épisode) | erreur de durée 11,8 → 5,0 min ; écart du début au début réel 5,5 min |
| Aucun rapport | `GET /reports/availability` (JSON, CSV) et panneau « Disponibilité » : indisponibilité (union des intervalles), dégradation, couverture des mesures, MTTD, MTTR | `tests/test_availability.py` |
| Une seule source réelle (SQL) | **Prometheus** (`query_range`, requêtes PromQL par sortie, curseur qui n'avance qu'après une réponse réussie) validée sur un vrai Prometheus 2.45 + node_exporter ; **Parquet** (lecture avec élagage par sortie) ; archive Parquet des mesures purgées | `tests/test_sources.py` (`NM_TEST_PROMETHEUS_URL`) |
| CSV lu en objets Python | CSV et Parquet adossés à **Polars** (colonnes, pas objets) | 1 M de lignes : ouverture 10,1 → 0,4 s, pic mémoire 749 → 226 Mo (CSV), 157 Mo (Parquet) |
| Pas d'image de déploiement | `Dockerfile` (front + API, utilisateur non privilégié, 1 worker), `docker-compose.yml`, `requirements.txt` d'exécution séparé de `requirements-dev.txt` | **image non construite ici (pas de Docker)** : étapes reproduites à la main, voir « Mesures » |


| Interface jamais vue dans un vrai navigateur | Playwright + Chromium contre le vrai backend (incident injecté) : 16 scénarios, dont infobulle à la souris, gzip/304/incrémental, onglet masqué, connexion, CSV téléchargé | `npm run e2e` ; les tests échouent quand on remet les défauts qu'ils gardent (vérifié) |
| Libellés d'axe tronqués (« .22 Gbps »), « maintenant » sur un libellé | marge gauche 50 → 68, libellé à gauche du trait | tests e2e de géométrie réelle |
| axe-core : bouton imbriqué dans un « bouton » (sérieux), contrastes 4,07:1 et 2,27:1 (sérieux) | un seul contrôle clavier par en-tête, `textDim`/`textFaint` éclaircis (≥ 4,5:1) | audit axe WCAG A/AA : aucune violation sérieuse ou critique |
| Polices chargées depuis Google | `@fontsource`, sous-ensemble latin, auto-hébergées (le dashboard garde son apparence sans Internet) | e2e : aucune requête hors origine |
| **Disponibilité de 99,456 % affichée pour 25 min d'observation** (calculée sur 30 jours) | période observée = depuis la première mesure conservée ; colonne « Observé » ; couverture recalculée | `tests/test_availability.py`, e2e (3 h 50 d'indisponibilité = la chute injectée) |
| Panneau d'alertes noyé de signaux faibles | `GET /alerts` ne rend que les alertes notables (`all=true` pour l'audit) | test API + e2e |
| Données Mock prises pour un vrai réseau | bandeau « Données simulées » + avertissement au démarrage hors développement | test unitaire, e2e |
| `PUT /config` renvoyait le texte brut de l'exception | erreurs structurées sans valeur saisie ni URL | `tests/test_api.py` |
| Une maintenance planifiée ou une panne aval alertait et dégradait la disponibilité | fenêtres de maintenance, inhibition amont/aval, alertes **retenues** (enregistrées, jamais notifiées) | `tests/test_maintenance_topology.py` (boucle réelle sur le Mock), e2e (aval rattaché à sa cause) |
| Aucune vérité terrain en exploitation | verdict vrai/faux positif, précision mesurée sur les incidents jugés, re-balayage `--from-db` | `tests/test_feedback_maintenance.py`, e2e (le verdict change précision et rapport, puis se retire) |
| Un moniteur qui s'arrête sans prévenir | `/metrics`, `/health/ready` (503 avec raisons), signal de vie externe, règles d'alerte d'exemple | `tests/test_observability.py` (serveur HTTP local), `promtool`, vrai Prometheus |
| **URL secrètes écrites dans le journal** : `httpx` journalise l'URL complète à INFO (jeton Telegram, signal de vie) | logger `httpx` relevé à WARNING | `tests/test_observability.py` — **échouait avant le correctif** |
| Une alerte sans réponse ne montait jamais : personne ne pouvait dire « je m'en occupe » | prise en charge (`PUT/DELETE /incidents/{id}/ack`) et escalade à un niveau (`notifications.escalate_after`) vers un second groupe | `tests/test_feedback_maintenance.py` (vraie boucle sur le Mock, 4 scénarios + table de conditions), `tests/test_incidents.py`, e2e |
| Tout utilisateur connecté pouvait tout faire, y compris modifier les seuils | trois rôles (lecteur, opérateur, administrateur) ; ancien format de `NM_USERS` conservé (= administrateur), nouveau format à rôle obligatoire, démarrage refusé si invalide | `tests/test_auth.py` (matrice rôle × endpoint, compatibilité, erreurs), Vitest, e2e à trois comptes |
| Avis de sécurité apparu pendant ce lot sur `vite` 8.3.0–8.3.2 (3 failles de `launch-editor`, serveur de développement ; deux concernent Windows) | `vite` 8.3.1 → 8.3.5 dans le lockfile (correctifs seulement) ; audit complet informatif ajouté à la CI | `npm audit` : 0 vulnérabilité ; typage, 61 Vitest et build revalidés |
| Messages d'alerte fixes, en français, sans possibilité de les adapter | langue par canal (fr, en) et modèles par événement, validés au chargement | `tests/test_notify.py` (catalogues, anglais, modèles, validation, envoi par canal), `tests/test_feedback_maintenance.py` (vraie boucle) |
| Aucune intégration continue | `.github/workflows/ci.yml` | ❓ jamais exécutée ; `actionlint` propre ; mêmes commandes reproduites en local |

## Mesures (Mock ; à refaire sur données réelles)

Protocole : `python -m network_monitor.analysis.evaluation --sweep`. Les anomalies de chaque exécution sont enregistrées une fois, puis rejouées à travers le moteur d'incidents avec chaque réglage (le score des anomalies ne dépend pas des seuils d'incident : le rejeu est exact). 8 jeux : 6 graines × 7 jours avec 12 scénarios chacune (8 types, 4 par sortie), plus 2 variantes de 6 jours (amplitude plus faible, période et rapport cyclique différents) — 96 scénarios, 162 sorties-jours. Réglage sur les graines 1-3 ; les graines 4-6 et les variantes ont servi à vérifier qu'un réglage ne colle pas à un seul jeu.

| Réglage (`minimum_duration` / `recovery_duration` / `min_suspect_steps`) | Détectés | Faux incidents | Par sortie-jour | Délai | Types justes |
|---|---|---|---|---|---|
| 10 min / 10 min / 2 (ancien) | 94/96 | 51 | 0,31 | ≈ 16 min | — |
| 15 min / 10 min / 2 | 94/96 | 37 | 0,23 | 20,6 min | 94,7 % |
| 20 min / 5 min / 4 | 85/96 | 1 | 0,006 | 26,1 min | 97,6 % |
| **15 min / 5 min / 3 (défaut)** | **87/96** | **7** | **0,04** | **20,5 min** | **96,6 %** |

- Les 9 scénarios manqués sont tous des intermittents de 90 min. Dans le cas inspecté, un seul échantillon de 5 min sur trois tombe dans la phase active : au pas de 5 min, c'est indiscernable du bruit. Passer `min_suspect_steps` à 2 les retrouve presque tous (90-94/96) mais ouvre 9 à 37 faux incidents.
- `open_score` (0,65-0,85) et `close_score` (0,3-0,5) changent au plus de 3 faux incidents sur l'ensemble : leurs valeurs par défaut sont conservées. `warning_score` n'a pas été balayé.
- Confusion de types restante : 3 sur 87 (2 dégradations progressives étiquetées « chute avec pertes », 1 « chute avec pertes » étiquetée progressive).
- Le Mock ne produit que des anomalies nettes, et les mêmes scénarios ont servi à écarter les réglages : les ordres de grandeur sont optimistes.

Isolation Forest (score 0-1, 0 = aussi habituel que 99 % de l'entraînement), moyenne par pas sur le palier de l'anomalie, 2 graines : chute soutenue 1,00 (normal : 0,05), rampe progressive 1,00, pertes élevées 1,00 ; intermittent 0,67 (les phases calmes sont normales par construction : minimum 0). Un seul premier pas de rampe n'est pas encore inhabituel (0,00). Il enrichit les preuves ; il n'ouvre jamais d'incident (mêmes incidents avec et sans, test).

### Mesures du lot « exploitation »

Trafic du dashboard, serveur réel, 3 sorties avec un graphe déplié chacune, un cycle = un rafraîchissement global + un rafraîchissement des 3 graphes :

| | Octets par cycle | Gain |
|---|---|---|
| avant (ni gzip, ni ETag, fenêtre entière) | 66,0 Ko | — |
| + gzip | 12,6 Ko | 5,3× |
| + ETag / 304 | 3,4 Ko | 19,6× |
| + incrémental (état actuel) | 1,7 Ko | 37,9× |

Passage à l'échelle de la calibration (Mock, 40 jours d'historique par sortie, Linux) :

| Sorties | Pic mémoire avant | Pic mémoire après | Calibration | Tick |
|---|---|---|---|---|
| 3 | 210 Mo | 205 Mo | 0,3 s | 9 ms |
| 30 | 405 Mo | 227 Mo | 3,3 s | 50 ms |
| 90 | 839 Mo | 275 Mo | 9,8 s | 130 ms |

Lecture d'un export de 1 036 800 lignes (90 sorties × 40 jours), processus isolés :

| Lecteur | Ouverture | 3 calibrations | Pic mémoire |
|---|---|---|---|
| CSV, objets Python (avant) | 10,1 s | 0,44 s | 749 Mo |
| CSV, Polars | 0,4 s | 0,16 s | 226 Mo |
| Parquet (zstd, 6,3 Mo contre 52 Mo de CSV) | 0,2 s | 0,27 s | 157 Mo |

Rétro-datage (8 jeux, 87 scénarios détectés) : écart moyen du début rétro-daté au début réel 5,5 min ; erreur moyenne de durée 11,8 min (ouverture → fermeture) → 5,0 min (début → fin rétro-datés). Biais signé de la fin : +3 à +7 min selon le type.

### Mesures du lot « maintenance, verdicts, supervision »

Suite backend contre de **vrais services** (MariaDB 10.11.14, Prometheus 2.45.3 avec node_exporter) : **245 passés, 0 ignoré** (222 passés + 6 ignorés avant ce lot, + 17 nouveaux). Frontend : 49 tests Vitest (+13), 16 scénarios Playwright (12 + 4 ; dont l'audit axe sur les nouveaux panneaux avec une vraie fenêtre de maintenance).

| Vérification | Résultat |
|---|---|
| `promtool check metrics` sur la sortie réelle de `/metrics` ; parseur officiel `prometheus_client` | accepté ; 13 familles lues |
| Vrai Prometheus qui scrape `/metrics` avec jeton porteur | `up = 1` ; `nm_ticks_total` 7 → 10 en 6 s (un pas / 2 s) ; 3 sorties calibrées ; mauvais jeton : cible `down`, HTTP 401 |
| `promtool check rules` / `promtool test rules` (5 cas) | 4 règles acceptées, tests passés ; **un bug trouvé** : avec `A and B`, `{{ $value }}` valait la valeur de A (`nm_monitoring_running` = 1 → « il y a 1 s ») ; opérandes inversés |
| Re-balayage `--from-db` (Mock, 1 incident long jugé vrai, 1 coupure de 20 min jugée fausse) | sur 162 réglages de la grille, 54 éliminent le faux positif en gardant le vrai, 108 le gardent ; sans réglage, le rejeu reproduit l'état d'origine |
| Tests de mutation | topologie retirée du serveur e2e → tests d'inhibition en échec ; faux positifs exclus non affichés → test de verdict en échec ; rapprochement temporel du rejeu cassé → 2 tests en échec ; tolérance de péremption × 100 → test de santé en échec |

### Mesure du point d'accès « mesure la plus proche »

Requêtes émises par le tableau « Comparatif temporel » à chaque rafraîchissement (3 sorties), comptées dans Chromium contre le vrai backend : **24** requêtes `history` bornées avant, **1** requête `GET /outputs/nearest` après. Le test navigateur correspondant échoue sur l'ancien code. Backend : 9 tests (dont fenêtre de recherche, « le plus proche » et non « le dernier », validation) ; Vitest 52, Playwright 17.

### Mesure de la prise en charge et de l'escalade

Suite backend contre de vrais services (MariaDB 10.11.14, Prometheus 2.45.3) : **265 passés, 0 ignoré**. Vitest : 57. Playwright : 19, dont deux scénarios sur le vrai serveur (escalade réelle d'une panne amont, une seule fois, jamais pour l'aval ; prise en charge dans l'interface qui persiste et empêche l'escalade au-delà du délai).

| Vérification | Résultat |
|---|---|
| Escalade sur la vraie boucle (Mock) | une seule alerte `escalated` par incident non pris en charge, entre 15 et 20 min après l'ouverture, reçue uniquement par le second groupe ; le groupe principal ne la reçoit pas |
| Tests de mutation (backend) | acquittement ignoré, garde « une seule fois » retirée, rétablissement ignoré, maintenance ignorée, délai ignoré : chacune fait échouer au moins un test |
| Test de mutation (navigateur, vrai serveur) | acquittements ignorés par le serveur → le scénario échoue (`escalated_at` renseigné au lieu de `null`) |
| **Erreur de ma part, corrigée** | ma première hypothèse (« un incident bref est déjà en retour à la normale à l'échéance ») était fausse : l'instrumentation de la boucle a montré qu'il reste ouvert et oscille. La condition « en retour à la normale » est donc testée sur chaque cas séparément (`_escalations`) et sur le moteur (`is_recovering`), pas par un scénario de bout en bout |

### Mesure des rôles

Backend : matrice complète rôle × endpoint (4 comptes × 10 endpoints protégés) ; compatibilité vérifiée par les tests d'authentification **déjà existants**, inchangés (71 passés avant toute nouvelle règle). Vitest : 61. Playwright : 22, avec un serveur déclarant trois comptes (`alice` en ancien format, `bob` opérateur, `carol` lectrice).

| Vérification | Résultat |
|---|---|
| Tests de mutation (backend) | endpoint opérateur ouvert aux lecteurs → 2 tests en échec ; vérification de rôle supprimée → au moins 4 tests en échec ; ancien format qui perd les droits d'administrateur → 3 tests en échec |
| Test de mutation (navigateur, vrai serveur) | rôles non vérifiés → les scénarios lecteur et opérateur échouent (200 reçu au lieu de 403) ; celui du compte historique passe, à raison |
| **Erreur de ma part, corrigée** | mon script de modification avait remplacé la signature de la dépendance `require_role` au lieu de celle de `start_monitoring` (chaîne présente deux fois) : récursion infinie à l'import, et `stop_monitoring` resté non protégé. Détecté par les tests, et par une vérification stricte de chaque signature ajoutée ensuite |
| **Formulation corrigée** | j'avais écrit qu'un changement de rôle « prend effet tout de suite » : vrai dans un test qui modifie l'environnement du processus, faux en exploitation, où il faut redémarrer. Docstrings et documentation corrigées |

### Mesure des messages multilingues

Refactorisation du formatage **sans changer le français** : la sortie de l'ancienne et de la nouvelle implémentation a été comparée sur une matrice exhaustive (8 événements × 3 sévérités × 7 types plus un type inconnu × 4 jeux de champs × 4 durées × présence des heures, en message isolé et regroupé, texte et SMS, 2 fuseaux) : **21 524 sorties, 0 différence**.

| Vérification | Résultat |
|---|---|
| Tests de mutation | canal qui ignore sa langue, modèles ignorés, validation des champs affaiblie, traduction oubliée, SMS non dépouillé de ses accents : chacun fait échouer au moins un test |
| Sécurité des modèles | `{output.__class__}`, `{output[0]}`, `{output!r}`, `{output:>20}`, champ inconnu, accolade mal fermée, modèle de plus de 600 caractères : tous refusés au chargement |

Image : sans Docker dans cet environnement, j'ai reproduit les étapes du `Dockerfile` à la main : build du front avec `VITE_API_BASE_URL=""` (le bundle ne contient plus l'adresse par défaut : même origine), environnement vierge avec le seul `requirements.txt` (aucun outil de dev, fuseau `Africa/Douala` disponible), démarrage en production sous un utilisateur sans privilèges. Constaté : `/` sert l'interface, le JS passe de 253 à 78 Ko en gzip, `/docs` et `/openapi.json` répondent 404, `PUT /config` répond 401 sans session et 200 avec, une seconde instance sur la même base est refusée avec un message explicite. Non vérifié : la construction de l'image elle-même, `HEALTHCHECK`, `read_only` et `cap_drop` du compose.

## Rectifications de la revue initiale

- « Suite de tests rouge (54/66) » : 66/66 passaient déjà sous Linux ; les échecs décrits ne sont pas reproduits ici (cause probable : Windows, non vérifié).
- « `.gitignore` absent » : il existait déjà.
- « Tick 1,5 s, gain 23× avec WAL » : non reproduit (16 ms/tick avec ou sans WAL) ; WAL est conservé pour la concurrence lecture/écriture.
- Ma première mesure « intermittent = 4 incidents ouverts » était **fausse** (le script réutilisait la même base pour 4 scénarios : compteurs cumulés). Réel avant correctif : 1 incident ouvert + 2-3 lignes de bruit par scénario.

## Non fait, ou à faire

La liste complète — limites connues, ce qui est vérifié ou non, fonctionnalités proposées mais non réalisées, décisions reportées (plan de
déploiement, phase 6), prochaines étapes avec leur critère de sortie — est dans **`docs/LIMITES_ET_FEUILLE_DE_ROUTE.md`**. Les trois points
à retenir avant toute exploitation réelle : (1) les alertes Telegram, SMS et webhook n'ont jamais été envoyées à de vrais services (webhook
seulement contre un vrai serveur local), (2) l'image Docker n'a jamais été construite, (3) tous les réglages viennent du Mock.
