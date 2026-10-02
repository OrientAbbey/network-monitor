# Architecture

Spécifications fonctionnelles : `CAHIER_DES_CHARGES_RESEAU.md` (les `§` des commentaires y renvoient).

## 1. Vue d'ensemble

Deux processus qui communiquent en HTTP/JSON :

```
Frontend (Vite/React, :5173)  ──HTTP──▶  Backend (FastAPI, :8000)
                                           ├─ MockDataSource      génère les mesures
                                           ├─ MonitoringLoop      valide, prévoit, détecte, gère les incidents (§30)
                                           └─ SqlAlchemyRepository  SQLite
```

Le frontend n'a aucune logique de détection : `src/api.js` est un client HTTP et `Dashboard.jsx` affiche ce que
l'API renvoie.

## 2. Backend

Le cœur est dans `data/`, `analysis/`, `incidents/`, `storage/`. `app/api.py` l'expose en HTTP.

### 2.1 Service (`app/api.py`)

- `MonitoringState` regroupe la configuration et la boucle, exécutée dans un thread dédié nommé
  `monitoring-loop`. `run_forever` est bloquant et n'a pas de rythme réel : l'API fait avancer l'horloge simulée
  (`seconds_per_tick` : 2 s réelles pour 5 min simulées).
- `DEFAULT_API_CONFIG` utilise 40 jours d'historique (contre 90 pour `AppConfig.defaults()`) : en dessous du seuil
  de sélection de modèle, la baseline saisonnière est retenue et le démarrage reste rapide.
- Le processus est unique : l'état (modèles, incidents en cours) est en mémoire, lancer `--workers 1`.
- CORS limité aux origines Vite locales (`NETWORK_MONITOR_CORS_ORIGINS` pour en ajouter). Pas d'authentification.

| Route | Rôle |
|---|---|
| `GET /health` | 200 si la boucle tourne et progresse, 503 avec la liste des problèmes sinon |
| `GET /outputs` | dernière mesure et statut de chaque sortie |
| `GET /outputs/{id}/history`, `/anomalies`, `/forecast`, `/alerts` | séries sur une fenêtre (31 jours maximum) |
| `GET /outputs/{id}/forecast/ahead` | prévision au-delà de l'instant courant, non persistée |
| `GET /outputs/{id}/snapshots` | comparatif temporel calculé côté serveur |
| `GET /incidents` | incidents sans leurs preuves (`IncidentSummary`) |
| `GET /incidents/{id}`, `/incidents/{id}/alerts` | incident complet, chronologie de ses alertes |
| `GET /monitoring/status`, `POST /monitoring/start`, `POST /monitoring/stop` | cycle de vie de la boucle |
| `GET /config`, `PUT /config` | `PUT` modifie à chaud les seuils et durées d'incident et `alerts.critical_score` |

Les datetimes sans fuseau sont lus comme UTC. `PUT /config` reconstruit un `IncidentConfig` complet : les validateurs
(§21 : `close_score < warning_score < open_score`) s'appliquent avant toute prise d'effet, et n'affectent que les
décisions futures.

`GET /outputs/{id}/snapshots` prend pour référence la dernière mesure de la sortie, pas l'horloge : une mesure est datée
du début de son intervalle, donc un pas en retard.

### 2.2 Boucle de monitoring (`app/main.py`)

- **Un tick** : récupérer les mesures, chercher les trous, valider, prévoir, scorer, mettre à jour les incidents,
  puis tout persister dans **une seule transaction** (`save_tick`).
- **Trous de données (§31)** : cherchés avant la validation, avec la borne `now - 2 * interval`.
- **Recalibration (§32)** : à l'échéance de `monitoring.recalibration_interval`, les modèles sont réajustés en excluant
  les périodes couvertes par un incident confirmé. Si plus de la moitié de l'historique d'une sortie serait exclue,
  son modèle est conservé. Un échec n'arrête pas la boucle.
- **Rétention** : `storage.retention` supprime mesures, prévisions et anomalies anciennes (`purge_before`). Incidents et
  alertes sont conservés.
- **Reprise après redémarrage** : à la première calibration, `IncidentManager.restore` reprend l'incident actif le plus
  récent de chaque sortie ; les orphelins plus anciens sont fermés. Le délai de récupération repart de zéro.

### 2.3 Incidents et alertes

- `Incident.score` est le pic du score depuis l'ouverture ; `current_score` est le dernier score, dont dérive `severity`.
- Les alertes (`Alert`, `AlertEvent`) sont liées au cycle de vie d'un incident et persistées dans la table `alerts`.
  Pas de canal externe (webhook, e-mail) : hors périmètre V1 (§38).

### 2.4 Stockage (`storage/repository.py`)

- SQLite en mode WAL (`synchronous=NORMAL`) : les lectures HTTP ne bloquent pas l'écriture de la boucle.
  `check_same_thread=False` : chaque appel ouvre sa propre session, seul l'engine est partagé.
- `list_incidents` et `list_active_incidents` acceptent `with_evidence=False` (lecture seule : un incident chargé ainsi
  ne doit jamais être réécrit par `upsert_incident`).
- `create_all` ne modifie pas une table existante : `SqlAlchemyRepository` ajoute automatiquement les colonnes
  nullables manquantes. Ni renommage ni suppression ; au-delà, utiliser Alembic.
- Pour un fort trafic concurrent, un SGBD serveur (PostgreSQL) serait plus adapté que SQLite.

### 2.5 Évaluation (`evaluation/`, §33)

`metrics.py` (fonctions pures) et `harness.py` exécutent le pipeline complet sur une suite de scénarios du Mock et
comparent détections et prévisions à la vérité terrain, sur une période distincte de la calibration :
`python -m network_monitor.evaluation`.

## 3. Frontend

- `src/api.js` : une fonction par endpoint. URL de base dans `VITE_API_BASE_URL` (`.env.example`). Chaque requête a un
  délai maximal de 8 s et peut être annulée par un `AbortSignal`.
- `src/lib.js` : fonctions pures (sous-échantillonnage min/max qui conserve les pics, formats, recul exponentiel).
- `src/Dashboard.jsx` :
  - **Rafraîchissement** : `/outputs`, `/incidents`, `/monitoring/status` toutes les 4 s, avec recul exponentiel (jusqu'à
    30 s) si l'API échoue ; graphe d'une sortie dépliée toutes les 5 s ; tableau comparatif toutes les 15 s, à raison
    d'une requête `/snapshots` par sortie. Les rafraîchissements sont suspendus quand l'onglet est caché.
  - **Pannes** : les données déjà affichées sont conservées avec un bandeau, et un indicateur donne l'âge de la dernière
    mise à jour réussie.
  - **Horloge** : les fenêtres des graphes sont calculées à partir de `simulated_now` et sans `until` (le serveur
    prend son horloge), jamais avec `new Date()`.
  - **Courbes** : Baseline et Prédiction ne sont demandées que si leur case est cochée. `EmptyRange` s'affiche quand une
    plage renvoie moins de 2 points (§4).
  - **Accessibilité** : landmarks, lien d'évitement, curseur clavier (`<input type="range">`), graphe décrit en texte,
    journal des incidents en `role="log"`.
  - **Configuration** : `SettingsPanel` envoie `PUT /config`. Les seuils de coloration du tableau restent locaux.
- `src/ErrorBoundary.jsx` affiche un message avec un bouton « Réessayer » si le rendu plante.

## 4. Pas d'historique avant le démarrage

`GET /outputs/{id}/history` ne renvoie que ce que la boucle a traité et stocké depuis le lancement de l'API.
`MockDataSource.load_history` peut produire une plage passée arbitraire (calibration initiale), mais ces données ne
sont jamais persistées. Avec le rythme accéléré :

| Plage demandée | Temps réel de fonctionnement nécessaire |
|---|---|
| 12 h | ~4,8 minutes |
| 1 jour | ~9,6 minutes |
| 1 semaine | ~1,1 heure |
| 1 mois | ~4,8 heures |

Le dashboard l'indique par un message explicite plutôt que par un graphe vide.
