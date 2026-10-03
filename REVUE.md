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
| Aucune authentification ; CORS `*` | jeton `NM_API_TOKEN` sur start/stop/config (503 si non configuré), CORS restreint (`NM_CORS_ORIGINS`), `/docs` masqué hors `development` | 401/422 vérifiés sur un serveur réel |
| `since` naïf → HTTP 500 ; plage non bornée | 422 si datetime naïf ; plage max 31 jours | tests |
| Champs de config sans effet (`detection.traffic`, `isolation_forest`, `extended_features`, `forecast.horizon`, `alerts.warning_score`) | supprimés ; `loss.enabled`, `forecast.models`, `history.minimum` câblés | grep : plus aucun champ orphelin |
| 3 champs d'anomalie perdus à l'écriture ; pas d'index ; pas de purge ; pas de `dispose()` | colonnes ajoutées, index `status`/`last_seen_at`, WAL, `purge_older_than`, `dispose()` | tests de repository |
| Recalibration (§32) absente ; historique rechargé N fois à la calibration | recalibration périodique excluant les incidents actifs ; un seul chargement | — |
| Bug clavier : Entrée/Espace sur le bouton interne déclenchait 2 bascules | garde `e.target === e.currentTarget` | lecture du code (non testé en navigateur) |
| Tests dépendants de `sleep`, de `mktemp`, connexions non fermées | attentes actives, `tmp_path`, `dispose()` | suite 44 s → 19 s |
| Dépendances inutilisées (polars, scipy, scikit-learn) | retirées | 0 import |
| Features §9 absentes | `analysis/features.py` (V1) et `features_extended.py` (§9.2, activable) ; V1 stockées avec chaque anomalie | `tests/test_features.py` |
| Aucun détecteur de confirmation (§17) | `IsolationForestDetector` optionnel : enrichit les preuves, ne décide jamais (mêmes incidents avec et sans) | test comparant deux exécutions |
| Une seule source (Mock) | `CsvDataSource` (rejeu) et `SqlDataSource` (table en lecture seule, identifiants validés, URL jamais renvoyée en clair) choisies par `source.type` | contrat commun testé sur les 3 sources |
| 5 commits par tick | `Repository.save_tick` : une transaction, un commit | test qui compte les commits |
| 39 erreurs mypy, 12 alertes ruff | tables SQLAlchemy en `Mapped[...]`, `Annotated` pour FastAPI | `mypy` et `ruff` sans erreur |
| Frontend : décimation, infobulle, repères, tests, TypeScript | décimation min/max (pics conservés), graphe lisible à la souris et au clavier, `header`/`main`/`section`, lien d'évitement, zones `aria-live`, panneau d'alertes ; `api.ts`/`types.ts`/`chart.ts` typés ; Vitest + Testing Library | `npm run typecheck && npm test` (16 tests) |
| ~10 faux incidents ouverts par jeu de 3 graines (7 j normaux) | `minimum_duration` 10 → 15 min, `recovery_duration` 10 → 5 min, `min_suspect_steps` = 3, choisis par balayage | tableau « Mesures » : 51 → 7 faux incidents sur 162 sorties-jours |
| Types `progressive_degradation` / `intermittent_degradation` jamais produits | motif temporel sur la magnitude de l'écart (alternance avec hystérésis, rampe corrélée), réévalué à chaque pas et figé pendant le retour à la normale ; un SUSPECT tolère les creux après `min_suspect_steps` pas actifs, pour qu'un intermittent reste un seul incident | exactitude des types 84/87 (96,6 %) sur 96 scénarios ; `tests/test_incident_patterns.py` |
| Pertes *inférieures* à l'habitude comptées comme anomalie (une sortie stable écrêtée à 0 % ouvrait un incident sévère) | seule une hausse des pertes est une anomalie (§25) | test |
| Seuils jamais balayés | banc `capture` / `replay` / `sweep` (rejeu exact des anomalies à travers le moteur d'incidents), jeu de scénarios standard et variantes | tableau ci-dessous |
| Isolation Forest qui ne séparait pas les paliers | deux forêts (niveau, dynamique), score = maximum, calibré sur l'entraînement, garde d'extrapolation | palier soutenu à 1,00 contre 0,05 en moyenne en normal |
| Source SQL non éprouvée sur MariaDB | testée sur MariaDB 10.11 réel (contrat, boucle de bout en bout, connexion coupée) ; `pool_pre_ping` ajouté (le test échoue sans) | `NM_TEST_MARIADB_URL` |
| Jeton en mémoire, pas d'authentification d'utilisateur | `POST /auth/login` : mots de passe scrypt (`NM_USERS`), jeton de session signé HMAC à expiration, 5 échecs / 5 min bloquent la connexion, identité journalisée ; jeton machine conservé | `tests/test_auth.py` (expiration, falsification, limitation) |
| `Dashboard.jsx` hors du typage ; infobulle à la souris non testée | `Dashboard.tsx` en TypeScript strict (`allowJs` retiré) ; calcul du pointeur extrait (`pointerToTime`) et testé, géométrie du SVG simulée | `npm run typecheck`, 24 tests Vitest |

| Dashboard : ~66 Ko par cycle de rafraîchissement, aucune compression | gzip (≥ 500 o), ETag + `Cache-Control: no-cache` (304 sans corps), rafraîchissement **incrémental** des graphes (`since` = dernier point connu, fusion par horodatage), polling suspendu quand l'onglet est masqué | 66,0 → 1,7 Ko par cycle (38×), voir « Mesures » |
| Plusieurs workers = plusieurs boucles sur la même base | verrou de fichier du noyau (`app/lock.py`) : refus de démarrer, y compris sur `--workers 2` (uvicorn s'arrête) et `WEB_CONCURRENCY > 1` ; libéré même après un `kill -9` | `tests/test_http_and_lock.py` (dont deux processus) |
| Calibration : tout l'historique de toutes les sorties en mémoire | `load_history(start, end, output_id)` : une sortie à la fois, libérée avant la suivante ; une sortie non calibrée au démarrage est réessayée chaque heure (et non chaque jour) | pic mémoire 405 → 227 Mo (30 sorties), 839 → 275 Mo (90) |
| Alertes stockées mais jamais envoyées | file d'envoi transactionnelle (écrite avec l'alerte), regroupement (`batch_window`), plafonds de messages, nouvelles tentatives à délai croissant, **Telegram** (groupe), **webhook** signé HMAC-SHA256, **SMS de groupe** (passerelle Android, API Orange) ; secrets dans l'environnement ; `POST /notifications/test/{canal}` | `tests/test_notify.py` (20 tests, dont un vrai serveur HTTP et un incident de bout en bout) |
| Début d'incident = date de confirmation | `started_at` (premier signal faible) et `ended_at` (premier pas sous `close_score`), rétro-datés ; un bruit juste après la fin d'un incident ne le prolonge plus (la fusion attend la confirmation du nouvel épisode) | erreur de durée 11,8 → 5,0 min ; écart du début au début réel 5,5 min |
| Aucun rapport | `GET /reports/availability` (JSON, CSV) et panneau « Disponibilité » : indisponibilité (union des intervalles), dégradation, couverture des mesures, MTTD, MTTR | `tests/test_availability.py` |
| Une seule source réelle (SQL) | **Prometheus** (`query_range`, requêtes PromQL par sortie, curseur qui n'avance qu'après une réponse réussie) validée sur un vrai Prometheus 2.45 + node_exporter ; **Parquet** (lecture avec élagage par sortie) ; archive Parquet des mesures purgées | `tests/test_sources.py` (`NM_TEST_PROMETHEUS_URL`) |
| CSV lu en objets Python | CSV et Parquet adossés à **Polars** (colonnes, pas objets) | 1 M de lignes : ouverture 10,1 → 0,4 s, pic mémoire 749 → 226 Mo (CSV), 157 Mo (Parquet) |
| Pas d'image de déploiement | `Dockerfile` (front + API, utilisateur non privilégié, 1 worker), `docker-compose.yml`, `requirements.txt` d'exécution séparé de `requirements-dev.txt` | **image non construite ici (pas de Docker)** : étapes reproduites à la main, voir « Mesures » |


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

Image : sans Docker dans cet environnement, j'ai reproduit les étapes du `Dockerfile` à la main : build du front avec `VITE_API_BASE_URL=""` (le bundle ne contient plus l'adresse par défaut : même origine), environnement vierge avec le seul `requirements.txt` (aucun outil de dev, fuseau `Africa/Douala` disponible), démarrage en production sous un utilisateur sans privilèges. Constaté : `/` sert l'interface, le JS passe de 253 à 78 Ko en gzip, `/docs` et `/openapi.json` répondent 404, `PUT /config` répond 401 sans session et 200 avec, une seconde instance sur la même base est refusée avec un message explicite. Non vérifié : la construction de l'image elle-même, `HEALTHCHECK`, `read_only` et `cap_drop` du compose.

## Rectifications de la revue initiale

- « Suite de tests rouge (54/66) » : 66/66 passaient déjà sous Linux ; les échecs décrits ne sont pas reproduits ici (cause probable : Windows, non vérifié).
- « `.gitignore` absent » : il existait déjà.
- « Tick 1,5 s, gain 23× avec WAL » : non reproduit (16 ms/tick avec ou sans WAL) ; WAL est conservé pour la concurrence lecture/écriture.
- Ma première mesure « intermittent = 4 incidents ouverts » était **fausse** (le script réutilisait la même base pour 4 scénarios : compteurs cumulés). Réel avant correctif : 1 incident ouvert + 2-3 lignes de bruit par scénario.

## Non fait, ou à faire

- **Seuils** : `warning_score` non balayé ; réglages mesurés sur le Mock seulement, qui ne contient ni dérive lente, ni panne partielle, ni saisonnalité changeante. Le bruit du Mock vient d'un générateur à état : deux `load_history` de la même plage ne rendent pas les mêmes valeurs.
- **Intermittents épars** : un seul échantillon actif sur trois n'est pas détecté par défaut ; un pas de mesure plus fin le résoudrait.
- **Notifications** : aucun envoi n'a été fait vers les vrais services (Telegram, API Orange, passerelle Android) : formats de requête vérifiés dans leur documentation et testés contre des serveurs simulés, un vrai serveur HTTP local pour le webhook. Premier essai réel : `POST /notifications/test/{canal}`. Garantie « au moins une fois » : une coupure entre l'envoi et son enregistrement peut doubler un message. Pas d'accusé de lecture, pas d'escalade, pas d'acquittement.
- **Prometheus** : validé sur un Prometheus 2.45 local avec node_exporter (compteurs d'interface de la machine), pas sur des compteurs SNMP d'équipements réels (`ifHCInOctets`) : les requêtes PromQL de la configuration sont à écrire et à vérifier pour chaque équipement.
- **Sources SQL** : MariaDB 10.11 local seulement ; ni PostgreSQL, ni un vrai Zabbix (connecteur non écrit : schéma `history_uint` connu par la documentation, pas essayé).
- **Rapport de disponibilité** : « indisponibilité » = incident de sévérité critique (réglable) ; pas de fenêtres de maintenance planifiée à exclure, pas de PDF, pas de rapport programmé par e-mail. Les indicateurs exacts exigés par le régulateur n'ont pas été vérifiés.
- **Authentification** : un seul niveau de droits, pas d'annuaire (SSO/LDAP), pas de révocation d'une session avant son expiration, limiteur d'essais en mémoire (réinitialisé au redémarrage, global derrière un proxy : lancer uvicorn avec `--proxy-headers` et `--forwarded-allow-ips`).
- **Image Docker** : non construite ici (voir ci-dessus). Pas de TLS ni de reverse proxy (volontairement laissés au plan de déploiement), pas de CI, pas de sauvegarde automatique de la base, pas de migrations (Alembic) : la base reste jetable à ce stade.
- **Frontend** : pas de test dans un vrai navigateur (jsdom : la géométrie est simulée, pas mesurée) ; pas d'audit d'accessibilité automatisé (axe).
