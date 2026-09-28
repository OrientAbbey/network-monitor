# 1. Objet du projet

Développer un système modulaire de **surveillance intelligente du trafic Data sur des sorties réseau**, capable de :

- recevoir des mesures de trafic et de pertes toutes les `5 minutes` par défaut ;
- fonctionner immédiatement avec un **générateur Mock** ;
- accepter ultérieurement une source réelle sans modifier le moteur d'analyse ;
- exploiter un historique d'au moins `30 jours` pour calibrer les modèles ;
- prévoir le trafic à court terme ;
- déterminer automatiquement le comportement normal de chaque sortie ;
- détecter les anomalies de trafic et de pertes sans données préalablement labellisées ;
- corréler plusieurs anomalies ;
- ouvrir, suivre et fermer des incidents ;
- générer des alertes explicites et compréhensibles ;
- mesurer les performances du système grâce aux scénarios contrôlés du Mock.

Le système V1 doit privilégier **robustesse, explicabilité, modularité et simplicité opérationnelle** plutôt que la complexité algorithmique.

---

# 2. Principes fondamentaux

## 2.1 Données non labellisées

Les données historiques sont exclusivement des observations brutes :

```text
timestamp
output_id
traffic_mbps
loss_percent
```

Aucune donnée ne contient :

```text
normal
anomaly
incident
```

Le comportement normal doit donc être appris statistiquement à partir de l'historique.

## 2.2 Distinction entre observation, anomalie et incident

Une observation est une mesure réelle.

Une anomalie est une observation ou une séquence inhabituelle.

Un incident est un ensemble d'anomalies suffisamment significatif, persistant et corrélé pour justifier une ouverture d'incident.

```text
Observation
    ↓
Analyse
    ↓
Anomalie éventuelle
    ↓
Corrélation + persistance + gravité
    ↓
Incident éventuel
```

Une anomalie ne doit donc pas automatiquement créer un incident.

---

# 3. Format canonique des données

Le format interne sera tabulaire et indépendant de la source.

```text
timestamp,output_id,traffic_mbps,loss_percent
2026-08-01T00:00:00,OUT_001,824.5,0.12
2026-08-01T00:00:00,OUT_002,12450.8,0.08
2026-08-01T00:05:00,OUT_001,831.2,0.11
```

Contrat logique :

```python
TrafficSample:
    timestamp: datetime
    output_id: str
    traffic_mbps: float
    loss_percent: float
```

Contraintes :

- `timestamp` obligatoire et timezone-aware ;
- `output_id` obligatoire ;
- `traffic_mbps >= 0` ;
- `0 <= loss_percent <= 100` ;
- une mesure par sortie et par timestamp ;
- fréquence configurable ;
- fréquence par défaut : `5 minutes`.

Le système doit accepter une fréquence différente, par exemple `1 min`, `10 min` ou `15 min`, sans modifier les composants métier.

---

# 4. Architecture fonctionnelle

```text
                    DATA SOURCE
                         │
              ┌──────────┴──────────┐
              │                     │
          Historique             Temps réel
              │                     │
              └──────────┬──────────┘
                         ▼
                Data Validation
                         │
                         ▼
                Feature Engineering
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
        FORECASTING              DETECTION
             │                       │
             └───────────┬───────────┘
                         ▼
                  INCIDENT ENGINE
                         │
                         ▼
                  ALERT MANAGEMENT
```

Le système doit être organisé autour de **trois contrats principaux** :

```text
DataSource
ForecastModel
AnomalyDetector
```

Le gestionnaire d'incidents reste indépendant des algorithmes utilisés.

---

# 5. Structure logicielle

Nombre volontairement réduit de fichiers :

```text
network_monitor/
│
├── app/
│   ├── main.py
│   ├── config.py
│   └── models.py
│
├── data/
│   ├── source.py
│   └── mock.py
│
├── analysis/
│   ├── forecasting.py
│   └── detection.py
│
├── incidents/
│   └── manager.py
│
├── storage/
│   └── repository.py
│
└── tests/
```

## Responsabilités

### `app/models.py`

Modèles de données communs :

- `TrafficSample`
- `ForecastResult`
- `AnomalyResult`
- `Incident`
- `IncidentEvidence`
- enums et structures communes.

### `data/source.py`

Interface `DataSource`.

Responsable de :

- récupération de l'historique ;
- récupération des nouvelles mesures ;
- normalisation vers le format canonique.

### `data/mock.py`

Implémentation Mock de `DataSource`.

Responsable de :

- génération de N sorties ;
- génération historique ;
- génération continue ;
- profils différents par sortie ;
- injection contrôlée d'incidents.

### `analysis/forecasting.py`

Contient :

- interface `ForecastModel` ;
- baseline saisonnière ;
- Holt-Winters ;
- calibration ;
- prévision ;
- intervalles prédictifs ;
- analyse des résidus.

### `analysis/detection.py`

Contient :

- baseline des pertes ;
- détection des anomalies de trafic ;
- détection des anomalies de pertes ;
- combinaison des signaux ;
- score d'anomalie ;
- règles de persistance.

### `incidents/manager.py`

Contient :

- `Incident` ;
- `IncidentEvidence` ;
- gestionnaire du cycle de vie ;
- corrélation ;
- ouverture ;
- mise à jour ;
- fermeture ;
- génération des alertes.

### `storage/repository.py`

Abstraction du stockage.

Elle ne doit pas être couplée au Mock ni aux algorithmes.

---

# 6. Source de données

Interface conceptuelle :

```python
class DataSource:

    def load_history(self, start, end):
        ...

    def get_latest(self):
        ...

    def get_interval(self):
        ...
```

Deux implémentations initiales :

```text
MockDataSource
RealDataSource
```

Le moteur d'analyse ne doit jamais dépendre directement de `MockDataSource`.

La future source réelle devra uniquement convertir ses données vers `TrafficSample`.

---

# 7. Générateur Mock

Le Mock constitue également l'environnement de validation du système.

Il doit simuler :

## Fonctionnement normal

- saisonnalité quotidienne ;
- saisonnalité hebdomadaire ;
- bruit ;
- variations naturelles ;
- heures de pointe ;
- heures creuses ;
- sorties de différentes tailles.

Exemple :

```text
OUT_001 → quelques centaines de Mbps
OUT_002 → quelques Gbps
OUT_003 → plusieurs dizaines de Gbps
```

Les amplitudes doivent être différentes afin d'empêcher le système de dépendre de seuils absolus.

## Scénarios d'anomalie

Le Mock doit pouvoir injecter :

1. baisse brutale du trafic ;
2. hausse anormale du trafic ;
3. augmentation des pertes ;
4. baisse du trafic + pertes élevées ;
5. dégradation progressive ;
6. incident intermittent ;
7. incident court ;
8. incident long ;
9. récupération progressive ;
10. combinaison de plusieurs anomalies.

Chaque scénario doit posséder une vérité terrain interne :

```text
scenario_id
output_id
start_time
end_time
type
severity
```

Cette vérité terrain est destinée uniquement à l'évaluation du système ; elle ne doit jamais être fournie au détecteur pendant son fonctionnement.

---

# 8. Historique et calibration

Historique minimal recommandé :

```text
30 jours
```

Historique préférable :

```text
60–90 jours
```

Avec 5 minutes :

```text
288 observations / jour / sortie
8 640 observations / 30 jours / sortie
17 280 observations / 60 jours / sortie
25 920 observations / 90 jours / sortie
```

Le modèle doit fonctionner avec 30 jours mais exploiter davantage d'historique lorsqu'il est disponible.

La calibration doit être effectuée **par sortie**, car les sorties peuvent avoir des échelles et comportements différents.

---

# 9. Feature Engineering

Les valeurs brutes restent disponibles, mais les modèles doivent principalement utiliser des variables contextualisées.

## 9.1 Features V1 (actives, calculées et stockées par défaut)

Seules les features réellement consommées par la détection statistique V1 sont calculées et stockées par défaut, afin de limiter la charge sur un environnement matériel contraint :

```text
traffic_mbps
loss_percent

traffic_vs_expected
loss_vs_expected

rolling_std_1h

hour
day_of_week
```

`rolling_std_1h` sert à la normalisation robuste des scores (voir §16). `hour` et `day_of_week` alimentent la Seasonal Baseline (§10).

## 9.2 Features étendues (désactivées par défaut, pour extensions futures)

Le reste des features envisageables n'est pas calculé en V1 tant qu'aucun composant ne les consomme (elles ne deviennent utiles qu'avec des détecteurs de type Isolation Forest ou équivalent, cf. §17/§36) :

```text
traffic_change_5m / 10m / 15m / 30m / 1h
loss_change_5m / 15m / 30m
rolling_mean_15m / rolling_mean_1h / rolling_median_1h
minute
```

Elles sont isolées dans un module dédié `analysis/features_extended.py`, activable via configuration, sans modifier le pipeline V1.

## 9.3 Transformation logarithmique du trafic

Pour gérer les différences d'échelle entre sorties (centaines de Mbps à dizaines de Gbps), le trafic est transformé en interne via `log1p(traffic_mbps)` (jamais `log()` nu, pour éviter toute explosion numérique si le trafic s'approche de 0 pendant un incident sévère).

Cette transformation ne s'applique qu'en interne aux modèles de forecasting (§10) :

- les résidus et écarts relatifs utilisés par la détection (§14) sont recalculés en espace Mbps réel après transformation inverse (`expm1()`), jamais en espace log ;
- les valeurs affichées à l'utilisateur (alertes, evidence) restent toujours en Mbps/Gbps.

Une prévision produite en espace log puis repassée en Mbps via `expm1()` est une estimation de la **médiane conditionnelle**, pas de la moyenne (effet Jensen sur une distribution proche du log-normal). C'est cohérent avec la préférence pour la médiane sur la moyenne énoncée en §10.A, et ne nécessite donc pas de correction de biais — mais cette propriété doit être documentée dans le code du module de forecasting pour éviter toute confusion future.

---

# 10. Forecasting V1

## Choix retenu

La V1 utilisera deux méthodes :

### A. Seasonal Baseline

Référence simple basée sur les observations historiques comparables.

Exemple :

```text
même sortie
même jour de semaine
même créneau
créneaux voisins
```

La médiane sera privilégiée à la moyenne lorsque cela améliore la robustesse.

### B. Holt-Winters / Exponential Smoothing

Modèle principal de forecasting V1.

Il permet de modéliser niveau, tendance et saisonnalité. Les méthodes d'Exponential Smoothing font partie des approches classiques de prévision de séries temporelles.

**Limite technique explicite :** l'implémentation Holt-Winters standard (`statsmodels.ExponentialSmoothing`) ne supporte qu'une seule période saisonnière. En V1, elle porte donc uniquement la **saisonnalité quotidienne** (`seasonal_periods = 288` avec un pas de 5 minutes). Elle ne capture pas l'effet hebdomadaire (semaine vs week-end, jours ouvrés vs non ouvrés).

L'effet hebdomadaire est porté exclusivement par la **Seasonal Baseline** (§10.A), qui compare explicitement même jour de semaine / même créneau. Le sélecteur de modèle (§11) doit donc être testé sur des jours de types différents (semaine et week-end) pour vérifier que la baseline compense correctement ce que Holt-Winters ne voit pas.

Le passage à un modèle multi-saisonnier (MSTL, TBATS) est envisageable en V2/V3 si cette limitation s'avère insuffisante, derrière la même interface `ForecastModel`.

Le système devra comparer la prévision Holt-Winters à la baseline saisonnière.

---

# 11. Sélection du modèle de forecasting

Le système ne doit pas supposer qu'un modèle est toujours supérieur.

Pour chaque sortie, les méthodes seront évaluées sur une portion historique réservée à la validation.

Métriques :

```text
MAE
RMSE
sMAPE
couverture des intervalles prédictifs
```

Le modèle présentant le meilleur comportement selon les critères configurés sera retenu.

Si Holt-Winters n'est pas suffisamment fiable pour une sortie, la baseline saisonnière pourra être utilisée.

**Règle sur l'historique disponible :** la sélection de modèle par backtesting nécessite de réserver une portion de l'historique pour la validation (par défaut les 7 derniers jours), ce qui réduit d'autant les données disponibles pour l'apprentissage. En dessous d'un seuil configurable de `model_selection.minimum_history` (valeur initiale : `45 jours`), la sélection par backtesting n'est pas effectuée et le système utilise automatiquement la Seasonal Baseline, plus robuste avec peu de données. Ce seuil doit être explicite plutôt que de laisser la sélection de modèle instable de manière silencieuse sous 45 jours d'historique.

---

# 12. Horizon de prévision

Valeur par défaut :

```text
30 minutes
```

Avec une fréquence de 5 minutes :

```text
6 prédictions
```

Exemple :

```text
18:35
18:40
18:45
18:50
18:55
19:00
```

L'horizon doit être configurable.

---

# 13. Résultat du forecasting

Chaque prédiction doit produire :

```python
ForecastResult(
    output_id,
    timestamp,
    predicted_traffic_mbps,
    lower_bound_mbps,
    upper_bound_mbps,
    confidence,
    model_name,
)
```

Exemple :

```text
Prévision : 18.1 Gbps
Intervalle attendu : 16.5–20.0 Gbps
```

L'intervalle est essentiel pour la détection d'anomalies.

Une mesure située à 18.8 Gbps est normale.

Une mesure située à 10.2 Gbps est potentiellement anormale.

---

# 14. Détection statistique V1

La détection primaire sera basée sur les **résidus du forecasting**.

```text
résidu = trafic_observé - trafic_prévu
```

et :

```text
résidu_relatif =
(trafic_observé - trafic_prévu) / trafic_prévu
```

Le système évaluera également si l'observation sort de l'intervalle attendu.

Une anomalie forte pourra donc être caractérisée par :

```text
écart important
+
hors intervalle attendu
+
persistance
```

---

# 15. Détection des pertes

Les pertes seront traitées indépendamment du trafic.

Le système construira pour chaque sortie une baseline historique des pertes :

```text
niveau habituel
dispersion habituelle
distribution temporelle
```

Il calculera notamment :

```text
loss_deviation
loss_change
loss_anomaly_score
```

**Plancher de dispersion :** certaines sorties ont historiquement une dispersion des pertes quasi nulle (ex. 0.0–0.1 % très stable). Sans précaution, un score basé sur l'écart-type ou le MAD historique diverge dès la moindre perte non nulle, produisant des faux positifs systématiques sur les sorties les plus fiables. La dispersion utilisée au dénominateur du score est donc bornée par un plancher configurable :

```text
loss_std_floor (valeur initiale : 0.05 point de %)
dispersion_utilisée = max(dispersion_historique, loss_std_floor)
```

Ainsi :

```text
Trafic normal
+
Pertes anormales
```

peut être détecté.

---

# 16. Score d'anomalie

La V1 doit produire plusieurs scores plutôt qu'un score opaque unique :

```text
traffic_anomaly_score
loss_anomaly_score
persistence_score
joint_anomaly_score
```

Puis éventuellement :

```text
incident_score
```

Chaque score doit être compris entre :

```text
0 = normal
1 = extrêmement anormal
```

## 16.1 Méthode de normalisation

Pour rester explicable et robuste aux distributions à queue épaisse (plutôt qu'un z-score classique, plus sensible aux valeurs extrêmes et moins interprétable), la normalisation retenue pour V1 est fondée sur la position de l'observation par rapport à l'intervalle prédictif ou à la dispersion historique robuste :

```text
score_trafic = min(1, |écart hors intervalle| / largeur de l'intervalle attendu)
score_pertes = min(1, |loss_deviation| / dispersion_utilisée)   # dispersion_utilisée : voir §15, plancher inclus
```

Cette formulation est directement interprétable ("à combien de largeurs d'intervalle on est sorti") et cohérente avec l'objectif d'explicabilité des §24 et §28. Elle est implémentée comme une fonction unique et partagée dans `analysis/detection.py`, réutilisée par le détecteur de trafic et le détecteur de pertes, afin de garantir que les différents scores restent comparables entre eux — condition nécessaire au calcul du `joint_anomaly_score`.

Les seuils et le choix de la dispersion de référence restent configurables.

---

# 17. Isolation Forest

Isolation Forest ne sera **pas le détecteur primaire de V1**.

Il sera prévu comme composant optionnel de validation/extension.

Il pourra utiliser :

```text
traffic_vs_expected
loss_vs_expected
traffic_change
loss_change
rolling statistics
```

Isolation Forest identifie les observations qui sont plus faciles à isoler par partitionnement aléatoire ; son score est donc une mesure d'anomalie indépendante des règles statistiques précédentes.

Son rôle futur :

```text
Détection statistique
        +
Isolation Forest
        ↓
confirmation / enrichissement
```

Il ne doit pas décider seul de l'ouverture d'un incident.

---

# 18. Pourquoi pas de LSTM/Transformer en V1 ?

Ils sont volontairement exclus de la première version.

Raisons :

- complexité inutile au démarrage ;
- entraînement plus coûteux ;
- calibration plus délicate ;
- explicabilité inférieure ;
- absence de données réelles labellisées ;
- nécessité de disposer d'un benchmark solide avant de justifier leur utilisation.

Ils pourront être ajoutés ultérieurement derrière la même interface :

```text
ForecastModel
AnomalyDetector
```

sans modifier le gestionnaire d'incidents.

---

# 19. Incident Engine

Le moteur d'incidents transforme les anomalies ponctuelles en événements temporels.

Cycle minimal :

```text
NORMAL
   ↓
SUSPECT
   ↓
OPEN
   ↓
ONGOING
   ↓
RECOVERING
   ↓
CLOSED
```

En interne, l'état est simplifié à trois valeurs effectivement utilisées par le moteur (NORMAL n'étant pas persisté) :

```text
SUSPECT
OPEN
CLOSED
```

avec les transitions déterminées par les règles du moteur.

**SUSPECT est un état à part entière, pas une simple absence d'incident.** Il est atteint dès que `score >= warning_score` (voir §26/§35) sans avoir encore franchi `open_score` pendant la durée minimale requise. Il permet de générer un signal précoce (alerte INFO) sans ouvrir formellement d'incident — voir §27 pour la cohérence avec la règle "toute alerte est liée à un événement d'incident".

---

# 20. Ouverture d'un incident

Une anomalie isolée ne doit pas immédiatement créer un incident.

Exemple de règle initiale configurable :

```text
incident_score >= 0.75
pendant au moins 10 minutes
```

Avec des mesures toutes les 5 minutes :

```text
18:00 anomalie
18:05 anomalie
18:10 anomalie
```

→ ouverture.

Ces paramètres ne doivent jamais être codés en dur.

---

# 21. Hystérésis

Le seuil d'ouverture doit être différent du seuil de fermeture.

Exemple initial :

```text
OPEN:
score >= 0.75 pendant 10 min

CLOSE:
score < 0.40 pendant 10 min
```

Cela évite les ouvertures/fermetures successives lorsque le score oscille autour d'un seuil.

**Durées adaptées par sortie.** Avec un pas de 5 minutes, une fenêtre fixe de 10 minutes ne représente que 2 points consécutifs : suffisant pour ouvrir un incident sur du simple bruit pour une sortie volatile, potentiellement trop lent pour une sortie critique très stable. Les durées `minimum_duration` et `recovery_duration` sont donc surchargeables par `output_id` dans la configuration (§35), en plus des valeurs globales par défaut. Une dérivation automatique à partir de la volatilité historique de chaque sortie (ex. `minimum_duration = base_duration * (1 + facteur * volatilité_normalisée)`) pourra être introduite ultérieurement derrière la même interface, sans être requise en V1.

---

# 22. Persistance

Le moteur doit suivre la durée d'une anomalie.

Exemple :

```text
18:00  normal
18:05  anomalie
18:10  anomalie
18:15  anomalie
18:20  anomalie
18:25  normal
```

L'incident sera :

```text
Ouverture : 18:05
Fermeture : 18:25
Durée     : 20 min
```

et non quatre incidents distincts.

---

# 23. Modèle Incident

Structure recommandée :

```python
Incident:
    id
    output_id

    status
    severity
    incident_type

    opened_at
    last_seen_at
    closed_at

    duration_seconds

    score

    peak_traffic_deviation
    peak_loss_percent

    description

    evidence
```

Types initiaux :

```text
TRAFFIC_DROP
TRAFFIC_SPIKE
HIGH_LOSS
TRAFFIC_DROP_WITH_HIGH_LOSS
PROGRESSIVE_DEGRADATION
INTERMITTENT_DEGRADATION
```

Le type doit être explicite et compréhensible.

---

# 24. IncidentEvidence

Chaque incident doit conserver les éléments permettant d'expliquer sa détection.

```python
IncidentEvidence:
    timestamp

    traffic_mbps
    expected_traffic_mbps

    traffic_deviation_percent

    loss_percent
    expected_loss_percent

    traffic_anomaly_score
    loss_anomaly_score
```

Exemple :

```text
18:20

Trafic réel       : 8.2 Gbps
Trafic attendu    : 15.4 Gbps
Écart             : -46.8 %

Pertes            : 7.4 %
Habituel          : 0.3 %

Anomalie trafic   : 0.94
Anomalie pertes   : 0.91
```

L'objectif est de rendre chaque incident **explicable**.

---

# 25. Corrélation trafic + pertes

Le moteur doit distinguer :

### Cas 1

```text
trafic ↓
pertes normales
```

→ anomalie trafic.

### Cas 2

```text
trafic normal
pertes ↑
```

→ anomalie pertes.

### Cas 3

```text
trafic ↓↓↓
pertes ↑↑↑
```

→ forte suspicion d'incident.

### Cas 4

```text
trafic ↑
pertes ↑
```

→ possible congestion/dégradation.

La combinaison des signaux doit augmenter la confiance sans supprimer les anomalies individuelles.

---

# 26. Sévérité

Trois niveaux initiaux :

```text
INFO
WARNING
CRITICAL
```

Exemple :

### INFO

Variation inhabituelle mais faible.

### WARNING

Anomalie significative et persistante.

### CRITICAL

Forte divergence + pertes importantes + persistance.

Les seuils doivent être configurables.

---

# 27. Alertes

Une alerte doit être liée à un événement d'incident.

Ne pas générer :

```text
ALERTE
ALERTE
ALERTE
ALERTE
```

toutes les 5 minutes.

Le comportement attendu est :

```text
ℹ️  SUSPECT (optionnel, alerte INFO)
       ↓
🚨 INCIDENT OUVERT
       ↓
MISE À JOUR
       ↓
MISE À JOUR
       ↓
RÉCUPÉRATION
       ↓
✅ INCIDENT FERMÉ
```

**Cohérence avec les seuils de configuration :** l'entrée en état `SUSPECT` (§19, `score >= warning_score`) est elle-même un événement du moteur d'incidents et peut donc générer une alerte de niveau INFO, sans contredire la règle ci-dessus. En revanche, aucune alerte WARNING ou CRITICAL ne peut être émise avant le passage effectif à l'état `OPEN` (`score >= open_score` pendant `minimum_duration`) : `warning_score` sert uniquement de seuil d'entrée en `SUSPECT`, jamais de déclencheur d'alerte indépendant de l'état du moteur.

Une alerte doit contenir :

```text
output
type
severity
start
durée
trafic attendu
trafic observé
écart
pertes
raison
```

---

# 28. Exemple d'alerte finale

```text
INCIDENT CRITIQUE — OUT_017

Ouverture : 18:10
Durée actuelle : 25 min

Trafic :
  attendu : 18.4 Gbps
  observé : 8.1 Gbps
  écart : -56 %

Pertes :
  habituel : 0.2 %
  actuel : 9.4 %

Motifs :
  • forte baisse du trafic
  • pertes anormalement élevées
  • anomalie persistante
  • divergence hors intervalle attendu
```

L'utilisateur doit comprendre le problème sans avoir besoin de connaître le fonctionnement interne du modèle.

---

# 29. Stockage

Le stockage doit séparer :

```text
mesures brutes
prévisions
anomalies
incidents
```

Les observations brutes ne doivent pas être copiées dans chaque incident.

L'incident ne conserve que ses preuves/résumés.

Cela permet d'éviter une explosion du volume de données.

---

# 30. Boucle temps réel

Toutes les `5 minutes` par défaut :

```text
1. récupérer les nouvelles données
2. valider
3. calculer les features
4. produire le forecast
5. comparer réel/prévision
6. analyser les pertes
7. calculer les scores
8. rechercher une corrélation
9. mettre à jour les incidents
10. générer les alertes
11. persister les résultats
```

La boucle ne doit pas dépendre directement de la fréquence de 5 minutes.

```text
monitoring.interval = 5m
```

doit être configurable.

---

# 31. Gestion des données manquantes

Le système doit détecter :

- timestamp manquant ;
- doublon ;
- valeur négative ;
- perte > 100 % ;
- sortie inconnue ;
- trous temporels ;
- données arrivant en retard.

Une donnée manquante ne doit pas être automatiquement remplacée par zéro.

Elle doit être marquée comme :

```text
MISSING
```

ou traitée par une stratégie d'imputation explicitement configurée.

**Effet sur le chrono d'hystérésis.** Une donnée `MISSING` ne doit ni faire progresser ni réinitialiser le chrono de persistance (§21/§22) : un incident déjà `OPEN` reste `OPEN` dans son état courant pendant l'absence de données, avec un indicateur `data_gap: true` ajouté à l'`IncidentEvidence` correspondante (§24), et le chrono ne reprend qu'à la prochaine mesure valide. Cela évite deux dérives symétriques : une fermeture prématurée d'incident causée par un simple trou de collecte, ou à l'inverse une ouverture artificielle déclenchée par une valeur aberrante isolée suivant un trou.

Une limite dure doit cependant être définie : si le trou dépasse une durée configurable (valeur initiale : `3 × l'intervalle de monitoring`), l'incident passe en état `STALE` plutôt que de rester `OPEN` indéfiniment sur la seule base de données absentes.

---

# 32. Dérive du modèle

Le comportement d'une sortie peut changer avec le temps.

Le système doit donc permettre une recalibration périodique.

V1 :

```text
calibration initiale
+
recalibration configurable
```

Par exemple :

```text
toutes les 24 h
```

La fréquence doit être configurable.

Les périodes identifiées comme fortement anormales ne doivent pas être aveuglément utilisées pour recalibrer le comportement normal.

---

# 33. Évaluation

Le Mock doit permettre une évaluation quantitative.

Pour chaque scénario connu :

```text
true_start
true_end
predicted_start
predicted_end
```

Mesures :

```text
Precision
Recall
F1
False Positive Rate
Detection Delay
Closure Delay
Duration Error
```

Pour le forecasting :

```text
MAE
RMSE
sMAPE
Prediction Interval Coverage
```

L'évaluation doit être réalisée sur des périodes différentes de celles utilisées pour la calibration.

---

# 34. Tests obligatoires

## Tests unitaires

- validation des mesures ;
- calcul des features ;
- calcul des résidus ;
- calcul des scores ;
- ouverture d'incident ;
- mise à jour d'incident ;
- fermeture ;
- hystérésis ;
- gestion des données manquantes.

## Tests d'intégration

```text
Mock
 ↓
Forecast
 ↓
Detection
 ↓
Incident
 ↓
Alert
```

## Tests de scénarios

Le système doit réussir au minimum :

```text
normal
traffic_drop
high_loss
traffic_drop_high_loss
traffic_spike
progressive_degradation
intermittent_incident
short_incident
long_incident
```

---

# 35. Configuration initiale

Exemple :

```yaml
monitoring:
  interval: 5m

history:
  minimum: 30d
  preferred: 90d

model_selection:
  minimum_history: 45d   # sous ce seuil : fallback automatique sur seasonal_baseline (§11)
  validation_window: 7d

forecast:
  horizon: 30m
  models:
    - seasonal_baseline    # porte l'effet hebdomadaire (§10.B)
    - holt_winters          # porte uniquement l'effet quotidien (seasonal_periods=288)

detection:
  traffic:
    enabled: true

  loss:
    enabled: true
    loss_std_floor: 0.05   # point de %, plancher de dispersion (§15)

  isolation_forest:
    enabled: false

  extended_features:
    enabled: false          # active analysis/features_extended.py (§9.2)

incident:
  open_score: 0.75
  close_score: 0.40
  warning_score: 0.50       # seuil d'entrée en état SUSPECT (§19), pas un déclencheur d'alerte indépendant
  minimum_duration: 10m
  recovery_duration: 10m
  stale_after: 15m          # 3 × interval : trou de données au-delà duquel un incident OPEN passe en STALE (§31)
  per_output_overrides: {}  # ex. { OUT_017: { minimum_duration: 15m } } — voir §21

alerts:
  enabled: true
  warning_score: 0.50   # aligné sur incident.warning_score — déclenche une alerte INFO à l'entrée en SUSPECT
  critical_score: 0.80
```

Les valeurs ci-dessus constituent des **valeurs initiales**, pas des vérités métier. Elles devront être calibrées grâce au Mock puis aux données réelles.

---

# 36. Évolution prévue

L'architecture doit permettre d'ajouter ultérieurement :

```text
Isolation Forest
HDBSCAN
Gradient Boosting
Autoencoder
LSTM Autoencoder
modèles avancés de forecasting
```

sans modifier :

```text
DataSource
IncidentManager
AlertManager
Storage
```

Le principe est :

```text
ForecastModel
    ├── SeasonalBaseline
    ├── HoltWinters
    └── FutureModel

AnomalyDetector
    ├── StatisticalDetector
    ├── IsolationForestDetector
    └── FutureDetector
```

---

# 37. Choix technologiques V1

Compte tenu de l'environnement du projet :

```text
Python
Polars
NumPy
SciPy
statsmodels
scikit-learn
FastAPI
SQLAlchemy
```

Polars sera privilégié pour les traitements tabulaires et historiques volumineux.

Les modèles statistiques seront isolés du reste de l'application afin de pouvoir les remplacer sans modifier l'architecture.

---

# 38. Choix définitifs de la V1

| Composant | Choix V1 | Alternative future |
|---|---|---|
| Source | Mock interchangeable | API/DB/fichier réel |
| Format | DataFrame / CSV logique | Source réseau native |
| Historique | ≥30 jours | 60–90 jours |
| Forecast | Seasonal Baseline + Holt-Winters | Gradient Boosting / modèles avancés |
| Horizon | 30 min | configurable |
| Détection trafic | Résidus forecast | Isolation Forest |
| Détection pertes | Baseline robuste | Autoencoder |
| Corrélation | Trafic + pertes + persistance | Ensemble ML |
| Incident | Moteur à états temporels | — |
| Alertes | liées aux incidents | canaux externes |
| Labels | aucun | incidents confirmés ultérieurement |
| ML supervisé | non | évolution future |
| Deep Learning | non | V3 éventuelle |

---

# 39. Principe architectural final

Le système ne doit jamais devenir :

```text
DONNÉE → IA → ALERTE
```

mais :

```text
                 DONNÉE
                    │
                    ▼
             CONTEXTUALISATION
                    │
                    ▼
              ┌─────────────┐
              │ FORECAST    │
              └──────┬──────┘
                     │
                     ▼
               RÉSIDU / ÉCART
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
     TRAFIC ANORMAL        PERTES ANORMALES
          │                     │
          └──────────┬──────────┘
                     ▼
                CORRÉLATION
                     │
                     ▼
                PERSISTANCE
                     │
                     ▼
              INCIDENT ENGINE
                     │
              ┌──────┴──────┐
              ▼             ▼
           OUVERTURE      FERMETURE
              │             │
              └──────┬──────┘
                     ▼
                  ALERTE
```

Le **forecasting fournit le comportement attendu**, le **détecteur identifie les écarts**, et le **moteur d'incidents transforme ces écarts en événements opérationnels explicables**.

C'est cette séparation qui doit être considérée comme le principe architectural central de la V1.