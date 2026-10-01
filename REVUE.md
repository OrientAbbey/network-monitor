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
| Types `progressive_degradation` / `intermittent_degradation` jamais produits | motif temporel sur la magnitude de l'écart (alternance avec hystérésis, rampe corrélée), réévalué à chaque pas et figé pendant le retour à la normale ; un SUSPECT tolère les creux après `min_suspect_steps` pas actifs, pour qu'un intermittent reste un seul incident | exactitude des types 85/87 (97,7 %) sur 96 scénarios ; `tests/test_incident_patterns.py` |
| Pertes *inférieures* à l'habitude comptées comme anomalie (une sortie stable écrêtée à 0 % ouvrait un incident sévère) | seule une hausse des pertes est une anomalie (§25) | test |
| Seuils jamais balayés | banc `capture` / `replay` / `sweep` (rejeu exact des anomalies à travers le moteur d'incidents), jeu de scénarios standard et variantes | tableau ci-dessous |
| Isolation Forest qui ne séparait pas les paliers | deux forêts (niveau, dynamique), score = maximum, calibré sur l'entraînement, garde d'extrapolation | palier soutenu à 1,00 contre 0,05 en moyenne en normal |
| Source SQL non éprouvée sur MariaDB | testée sur MariaDB 10.11 réel (contrat, boucle de bout en bout, connexion coupée) ; `pool_pre_ping` ajouté (le test échoue sans) | `NM_TEST_MARIADB_URL` |
| Jeton en mémoire, pas d'authentification d'utilisateur | `POST /auth/login` : mots de passe scrypt (`NM_USERS`), jeton de session signé HMAC à expiration, 5 échecs / 5 min bloquent la connexion, identité journalisée ; jeton machine conservé | `tests/test_auth.py` (expiration, falsification, limitation) |
| `Dashboard.jsx` hors du typage ; infobulle à la souris non testée | `Dashboard.tsx` en TypeScript strict (`allowJs` retiré) ; calcul du pointeur extrait (`pointerToTime`) et testé, géométrie du SVG simulée | `npm run typecheck`, 24 tests Vitest |

## Mesures (Mock ; à refaire sur données réelles)

Protocole : `python -m network_monitor.analysis.evaluation --sweep`. Les anomalies de chaque exécution sont enregistrées une fois, puis rejouées à travers le moteur d'incidents avec chaque réglage (le score des anomalies ne dépend pas des seuils d'incident : le rejeu est exact). 8 jeux : 6 graines × 7 jours avec 12 scénarios chacune (8 types, 4 par sortie), plus 2 variantes de 6 jours (amplitude plus faible, période et rapport cyclique différents) — 96 scénarios, 162 sorties-jours. Réglage sur les graines 1-3 ; les graines 4-6 et les variantes ont servi à vérifier qu'un réglage ne colle pas à un seul jeu.

| Réglage (`minimum_duration` / `recovery_duration` / `min_suspect_steps`) | Détectés | Faux incidents | Par sortie-jour | Délai | Types justes |
|---|---|---|---|---|---|
| 10 min / 10 min / 2 (ancien) | 94/96 | 51 | 0,31 | ≈ 16 min | — |
| 15 min / 10 min / 2 | 94/96 | 37 | 0,23 | 20,6 min | 94,7 % |
| 20 min / 5 min / 4 | 85/96 | 1 | 0,006 | 26,1 min | 97,6 % |
| **15 min / 5 min / 3 (défaut)** | **87/96** | **7** | **0,04** | **20,5 min** | **97,7 %** |

- Les 9 scénarios manqués sont tous des intermittents de 90 min. Dans le cas inspecté, un seul échantillon de 5 min sur trois tombe dans la phase active : au pas de 5 min, c'est indiscernable du bruit. Passer `min_suspect_steps` à 2 les retrouve presque tous (90-94/96) mais ouvre 9 à 37 faux incidents.
- `open_score` (0,65-0,85) et `close_score` (0,3-0,5) changent au plus de 3 faux incidents sur l'ensemble : leurs valeurs par défaut sont conservées. `warning_score` n'a pas été balayé.
- Confusion de types restante : 2 dégradations progressives étiquetées « chute avec pertes ».
- Le Mock ne produit que des anomalies nettes, et les mêmes scénarios ont servi à écarter les réglages : les ordres de grandeur sont optimistes.

Isolation Forest (score 0-1, 0 = aussi habituel que 99 % de l'entraînement), moyenne par pas sur le palier de l'anomalie, 2 graines : chute soutenue 1,00 (normal : 0,05), rampe progressive 1,00, pertes élevées 1,00 ; intermittent 0,67 (les phases calmes sont normales par construction : minimum 0). Un seul premier pas de rampe n'est pas encore inhabituel (0,00). Il enrichit les preuves ; il n'ouvre jamais d'incident (mêmes incidents avec et sans, test).

## Rectifications de la revue initiale

- « Suite de tests rouge (54/66) » : 66/66 passaient déjà sous Linux ; les échecs décrits ne sont pas reproduits ici (cause probable : Windows, non vérifié).
- « `.gitignore` absent » : il existait déjà.
- « Tick 1,5 s, gain 23× avec WAL » : non reproduit (16 ms/tick avec ou sans WAL) ; WAL est conservé pour la concurrence lecture/écriture.
- Ma première mesure « intermittent = 4 incidents ouverts » était **fausse** (le script réutilisait la même base pour 4 scénarios : compteurs cumulés). Réel avant correctif : 1 incident ouvert + 2-3 lignes de bruit par scénario.

## Non fait, ou à faire

- **Seuils** : `warning_score` non balayé ; réglages mesurés sur le Mock seulement, qui ne contient ni dérive lente, ni panne partielle, ni saisonnalité changeante.
- **Intermittents épars** : un seul échantillon actif sur trois n'est pas détecté par défaut (voir ci-dessus) ; un pas de mesure plus fin le résoudrait.
- **Sources réelles** : validées sur MariaDB 10.11 local avec des tables de test ; aucun essai sur un réseau réel, ni sur un autre moteur (PostgreSQL). La source SQL suppose des horodatages UTC sans fuseau.
- **Authentification** : un seul niveau de droits (tout utilisateur connecté peut tout modifier), pas d'annuaire (SSO/LDAP), pas de révocation d'une session avant son expiration, limiteur d'essais en mémoire (réinitialisé au redémarrage, global derrière un proxy).
- **Frontend** : pas de test dans un vrai navigateur (jsdom : la géométrie est simulée, pas mesurée) ; pas d'audit d'accessibilité automatisé (axe).
- **Non traités** : polices Google auto-hébergées, transport d'alertes (webhook/SSE), Docker/CI (phase 6, exclue), Alembic (base jetable à ce stade).
