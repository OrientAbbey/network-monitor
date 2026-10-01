# Plan de correction : bilan

Suite de `REVUE.md`. Ce document recense ce qui a été corrigé, les défauts trouvés en cours de route, les erreurs de la revue initiale et les mesures réalisées.

## Vérifications

- 161 tests backend, 37 tests frontend, `ruff` et `mypy` sans erreur.
- `pip install -e ".[dev]"` et `npm ci` depuis un clone vierge, puis build du frontend.
- Les tests de non-régression ont été vérifiés en échec sur le code d'origine (backend : 17 échecs et 3 erreurs dans `test_regressions.py` ; frontend : tous les tests dépendant du `Dashboard` échouent sur l'ancien composant).
- Contrat frontend/backend vérifié contre un vrai serveur `uvicorn`.

Non vérifié : rendu dans un navigateur réel, Python 3.11 (seul 3.12 était disponible), exécution de la CI sur GitHub.

## Avancement

| Item | État |
|---|---|
| Installation du frontend à froid (`@vitejs/plugin-react ^5`) | Fait |
| Suite de tests déterministe (attentes actives, `dispose()`, `tmp_path`) | Fait |
| SQLite : WAL, une transaction par tick, index sur `incidents` | Fait |
| CORS restreint, fenêtres bornées à 31 jours, datetimes naïfs lus comme UTC | Fait |
| Validation des seuils (`close < warning < open`) et des durées | Fait |
| `stop()` avec `join`, verrou sur `MockDataSource` | Fait |
| Rétention (`storage.retention`, 30 jours par défaut) | Fait |
| `ruff`, `mypy`, CI GitHub Actions | Fait (`ruff format` non imposé : il reformaterait 17 fichiers) |
| Configuration : chaque option est câblée ou refusée explicitement | Fait |
| Déviations d'anomalie persistées, migration additive des bases existantes | Fait |
| Holt-Winters : `predict()` respecte `from_time`, série régularisée | Fait |
| `Incident.current_score` (`score` reste le pic) | Fait |
| Recalibration périodique (§32) | Fait |
| Évaluation quantitative (§33) | Fait |
| Alertes persistées et exposées (§27), sans canal externe (hors V1, §38) | Fait |
| Frontend : horloge, accessibilité, réseau, sous-échantillonnage, curseur, tests | Fait |
| Backtest glissant | Évalué, non implémenté (voir Mesures) |

## Défauts trouvés et corrigés

**Trou de données fantôme à chaque tick.** `missing_slots(output, now)` était appelé après `validate_batch`. L'échantillon d'un tick est daté `now - interval`, donc le créneau `now` était signalé manquant à chaque tick et pour chaque sortie. Sur un incident scripté de 40 minutes, 14 des 31 preuves stockées étaient fantômes. Correctif : chercher les trous avant `validate_batch`, avec la borne `now - 2 * interval`. Changer seulement la borne masque les vrais trous. Tests : `test_no_phantom_missing_slot_in_normal_operation`, `test_real_gap_is_detected_exactly_once`.

**Validation des seuils incomplète.** `IncidentConfig` ne vérifiait que `close < open` : `warning_score` pouvait valoir n'importe quoi, ce qui laissait une plage de scores sans effet. Un validateur d'ensemble impose `close < warning < open`.

**Durées négatives acceptées.** `_parse_duration("-5m")` passait, et `"0"` était refusé. Une `minimum_duration` négative contournait l'hystérésis.

**Doublon traité comme un trou.** Un doublon (`DUPLICATE`) suivait la branche `data_gap` et ajoutait une fausse preuve à un incident actif. Il est désormais ignoré.

**Holt-Winters constant.** `predict()` appelait `forecast(n_steps)`, qui prévoit toujours depuis la fin de l'entraînement, sans jamais lire `from_time` : la prévision à 1 pas était identique à toute heure. Après correctif, à 0 h/6 h/12 h/18 h : 193/234/398/356 Mbps (baseline : 194/240/402/361). La série est aussi régularisée (créneaux absents interpolés), car un trou décalerait la saisonnalité.

**`score` et sévérité incohérents.** `score` est un pic, `severity` dérive du dernier score : un incident pouvait afficher « score 0.90 » avec la sévérité `warning`. Ajout de `current_score`.

**Horloge du frontend.** Le frontend calculait ses fenêtres avec `new Date()`, alors que le serveur horodate en horloge simulée (300 s par tick de 2 s, soit 150 fois le temps réel). Après 40 s, une fenêtre de 12 h ne renvoyait que 1 point sur 20 disponibles. Les graphes lisent désormais `simulated_now` et omettent `until`.

**Colonne « Actuelle » du tableau comparatif.** Une mesure est datée de son intervalle, donc un pas en retard sur l'horloge : avec l'horloge comme référence, les décalages 0 et 5 min désignaient la même mesure et la tendance était toujours plate. `GET /outputs/{id}/snapshots` prend la dernière mesure comme référence et remplace 24 requêtes par 3.

**Incidents orphelins après redémarrage.** `IncidentManager` démarrait vide : un incident ouvert au moment de l'arrêt restait « actif » en base, jamais suivi ni fermé (1 actif en base, 0 en mémoire après 3 h 20 de fonctionnement normal), et `/outputs` l'affichait ouvert indéfiniment. Chaque redémarrage pendant un incident en laissait un de plus. `IncidentManager.restore()` reprend au premier démarrage l'incident actif le plus récent de chaque sortie (preuves conservées, pas de doublon) et ferme les orphelins déjà présents (`closed_at` = dernière observation). Le délai de récupération repart de zéro : un incident peut rester ouvert un peu plus longtemps après un redémarrage, jamais se fermer trop tôt. Tests : `test_restart.py`.

**`GET /incidents` trop lourd.** Chaque incident embarquait ses preuves (~300 octets par pas de temps) : 25 191 octets pour 8 incidents, rechargés toutes les 4 s par le dashboard (~0,5 Go par jour et par onglet ouvert). La liste renvoie maintenant `IncidentSummary` (sans preuves, et sans même les lire en base) ; `GET /incidents/{id}` les donne. Mesuré après correctif : 2 950 octets pour 7 incidents. Changement d'API : la clé `evidence` n'existe plus dans la liste.

**`/health` aveugle.** Répondait « ok » même boucle arrêtée. Il répond 503 avec la liste des problèmes si la boucle est arrêtée, son thread mort, 3 ticks consécutifs échouent, ou si aucun tick n'a réussi depuis max(3 × `seconds_per_tick`, 60 s). Une boucle arrêtée volontairement via `POST /monitoring/stop` donne donc aussi 503.

**Fichiers annexes WAL non ignorés.** Le mode WAL crée `*.db-wal` et `*.db-shm`, que le `.gitignore` ne couvrait pas (régression introduite par ce travail, corrigée).

## Rectificatifs

- **REVUE.md, P0-4 :** un `.gitignore` complet existe déjà.
- **REVUE.md, outillage :** une `LICENSE` (MIT) existe déjà.
- **REVUE.md, tests :** la suite passe entièrement sur Linux. Les échecs décrits dépendent de Windows (verrou SQLite) et de la vitesse du disque. Les défauts structurels (pas de `dispose()`, `sleep` fixes) restaient réels.
- **Gain de performance du WAL :** le chiffre « ~23x » vient de REVUE.md, je ne l'ai pas mesuré.
- **Mon premier diagnostic du Holt-Winters plat était faux :** je l'attribuais à la non-convergence de statsmodels ; la cause est `from_time` ignoré. La non-convergence est seulement journalisée.

## Mesures

**Banc d'évaluation (`python -m network_monitor.evaluation`), défauts actuels, 6 graines, 10 scénarios chacune.** Rappel 0,93 ; précision 0,82 ; 2,2 faux positifs par 2 jours ; délai de détection moyen 9,9 min ; scénario manqué : `intermittent_incident`. Les intervalles de prévision sont calibrés (couverture de 79 % pour 80 % nominal).

| Variante (6 graines) | Rappel | Précision | Faux positifs / 2 j | Délai moyen | Manqués |
|---|---|---|---|---|---|
| Défauts (`open` 0,75, `minimum_duration` 10 min) | 0,93 | 0,82 | 2,2 | 9,9 min | intermittent |
| `minimum_duration` 15 min | 0,82 | 0,98 | 0,2 | 15,3 min | intermittent, short |
| `minimum_duration` 20 min | 0,82 | 1,00 | 0,0 | 20,3 min | intermittent, short |
| `open_score` 0,85 | 0,93 | 0,85 | 1,8 | 9,9 min | intermittent |
| `open_score` 0,85 et `minimum_duration` 15 min | 0,82 | 0,98 | 0,2 | 15,4 min | intermittent, short |

Allonger `minimum_duration` supprime presque tous les faux positifs, mais rend le scénario de 15 minutes indétectable par construction. Les défauts n'ont pas été modifiés : le choix dépend de l'usage et doit se faire sur des données réelles, pas sur un Mock à graine fixe.

**Backtest (2 graines, 2 sorties, 90 jours d'historique).** La méthode actuelle (une origine, 7 jours d'un coup) et un backtest glissant (3 origines, horizon 24 h) désignent la baseline dans les 4 cas. L'erreur de Holt-Winters est 4 à 5 fois supérieure (par exemple 52 contre 12 Mbps sur `OUT_001`). Le glissant coûterait 20 à 35 s d'ajustement Holt-Winters par sortie et par calibration, sans changer de décision. À reconsidérer si une source réelle rend Holt-Winters compétitif.

## Non traité

- Authentification de l'API.
- Types TypeScript générés depuis OpenAPI.
- Colonnes SQLAlchemy en `Mapped[...]` (des exceptions `mypy` ciblent `storage/repository.py`).
- Auto-hébergement des polices.
- Réglage des seuils d'incident sur des données réelles.
