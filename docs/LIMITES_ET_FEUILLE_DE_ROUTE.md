# Limites, fonctionnalités proposées et décisions reportées

Ce document dit **ce que le système ne fait pas, ce qui n'a pas été vérifié, ce qui a été proposé mais pas réalisé,
et ce qui a été reporté volontairement**. Il complète `ARCHITECTURE.md` (ce qui est) et `../REVUE.md` (ce qui a été
corrigé et mesuré). Rien n'y est caché derrière un « à peu près » : quand un point est incertain, il est écrit comme tel.

**Légende** — ✅ vérifié contre le vrai service ou dans un vrai navigateur · 🧪 testé contre une simulation ·
❓ non vérifié · ⛔ non fait · ⏸ reporté à la demande du propriétaire du projet.

## 1. Ce qui a été validé, et comment

| Élément | Niveau | Détail |
|---|---|---|
| Interface dans un navigateur | ✅ | Chromium piloté par Playwright, contre le **vrai** backend (incident injecté), sur la même origine que l'image Docker : infobulle à la souris, libellés d'axe, pas de débordement à 375 px, gzip/304/incrémental vus par le navigateur, polling suspendu onglet masqué, connexion, rapport et téléchargement CSV, audit axe-core WCAG A/AA. Les tests de la marge d'axe et du libellé « maintenant » échouent bien quand on remet les défauts (vérifié). |
| Accessibilité | ✅ (automatisé) | axe-core ne détecte plus de violation sérieuse ou critique. **Un audit automatisé ne couvre qu'une partie de WCAG** : pas d'essai avec un lecteur d'écran, pas d'essai au zoom 200 %. |
| MariaDB (source SQL) | ✅ | MariaDB 10.11 locale : contrat de la source, boucle de bout en bout, reprise après une connexion coupée. |
| Prometheus (source) | ✅ partiel | Prometheus 2.45 local avec node_exporter : historique, curseur live, boucle. **Compteurs d'interface de la machine de test, pas des compteurs SNMP d'équipements.** |
| Webhook | ✅ | Vrai serveur HTTP local : signature HMAC-SHA256 vérifiée, identifiant de livraison stable, nouvelle tentative après un 500. |
| Telegram | 🧪 | Transport simulé. Format de requête vérifié dans la documentation de l'API, jamais envoyé à un vrai groupe. |
| SMS (passerelle Android, API Orange) | 🧪 | Transports simulés. Formats vérifiés dans la documentation de chaque service, jamais envoyés à un vrai numéro. |
| Maintenance, inhibition amont/aval, verdicts | 🧪 + ✅ | Vraie boucle de surveillance sur le Mock (incidents injectés) et vrai navigateur : aval rattaché à sa cause et retenu, verdict qui change précision et rapport, maintenance créée puis annulée. Jamais sur un vrai réseau. |
| `/metrics`, règles d'alerte | ✅ | `promtool check metrics` et `check rules`/`test rules` ; vrai Prometheus 2.45 qui scrape avec jeton porteur. Les seuils des règles sont des exemples. |
| Signal de vie | 🧪 | Vrai petit serveur HTTP local (succès, échec, 500, service injoignable, arrêt). **Jamais contre un vrai Healthchecks.io.** |
| CI GitHub Actions | ❓ | Écrite et validée par `actionlint`, **jamais exécutée**. Commandes backend reproduites en local avec MariaDB et Prometheus réels. Le job Docker, le job Windows et `npx playwright install --with-deps` n'ont jamais tourné. |
| Image Docker | ❓ | **Jamais construite** (pas de Docker dans l'environnement de développement). Étapes reproduites à la main : build du front en même origine, environnement vierge avec le seul `requirements.txt`, démarrage en production sous un utilisateur sans privilèges, verrou d'instance. Non vérifié : la construction, `HEALTHCHECK`, `read_only`, `cap_drop`. |
| Réseau réel | ❓ | Aucune mesure d'un vrai réseau n'est entrée dans le système. Tous les réglages viennent du Mock. |

## 2. Limites connues du système

### 2.1 Détection

- **Réglages établis sur le Mock seulement** (8 jeux, 96 scénarios, 162 sorties-jours). Le Mock ne contient que des anomalies
  nettes : pas de dérive lente, pas de panne partielle, pas de saisonnalité qui change, pas de bruit réel d'équipement.
  Les chiffres (0,04 faux incident par sortie et par jour, 87 scénarios sur 96) sont **optimistes** : les mêmes scénarios ont servi à
  écarter les mauvais réglages. À refaire sur des mesures réelles (§5).
- **Intermittents épars non détectés** : les 9 scénarios manqués sur 96 sont tous des intermittents dont un seul échantillon de 5 min
  sur trois tombe dans la phase active, indiscernable du bruit à ce pas. `min_suspect_steps = 2` les retrouve presque tous au prix de
  9 à 37 faux incidents.
- **`warning_score` jamais balayé** ; `open_score` et `close_score` l'ont été (≤ 3 faux incidents d'écart sur l'ensemble).
- **Délai de détection ≈ 20 min** (15 min de confirmation + un pas) ; le **début rétro-daté** est à 5,5 min en moyenne du début réel ;
  la **fin** a un biais de +3 à +7 min selon le type.
- **Types d'incident** : 84 justes sur 87 ; 3 confusions (2 progressifs étiquetés « chute avec pertes », 1 l'inverse). Les types
  « progressif » et « intermittent » reposent sur des seuils de motif (`_PATTERN_*` dans `incidents/manager.py`) réglés sur le Mock.
- **Isolation Forest** : optionnel (nécessite `scikit-learn`), il enrichit les preuves et n'ouvre jamais un incident. Mesuré sur 2 graines :
  palier soutenu 1,00 contre 0,05 en moyenne en normal ; intermittent 0,67 en moyenne (les phases calmes sont normales : minimum 0). Son
  calibrage par le 99e centile de l'entraînement suppose un historique d'entraînement sans anomalie : un historique pollué le rend aveugle.
- **Pas d'historique avant le démarrage** : la calibration n'est pas persistée, et un graphe ne montre que ce que la boucle a traité.
- **Une seule métrique par sortie** (trafic et pertes) ; pas de latence, de gigue, de CPU d'équipement, ni de corrélation entre sorties.

### 2.2 Données

- **Mock** : bruit tiré d'un générateur à état ; deux `load_history` de la même plage ne rendent pas les mêmes valeurs (les tests ne doivent
  jamais supposer le contraire).
- **Source par défaut = Mock.** Sans `NM_CONFIG`, **le serveur affiche des données simulées même en production**. Garde-fous ajoutés :
  bandeau « Données simulées » dans l'interface et avertissement au démarrage si `NM_ENV` n'est pas `development`. Ce n'est pas une
  interdiction : un déploiement mal configuré démarre quand même.
- **Prometheus** : les requêtes PromQL sont à écrire et à valider **pour chaque équipement** (compteurs 64 bits `ifHCInOctets`, `rate()`,
  étiquettes). Une requête qui renvoie plusieurs séries est refusée (erreur claire). Un compteur 32 bits boucle en ~34 s à 1 Gbps (calcul) :
  inutilisable.
- **SQL** : horodatages supposés en UTC sans fuseau ; testé sur SQLite et MariaDB 10.11, **pas sur PostgreSQL** ; aucun connecteur Zabbix
  (§3).
- **CSV/Parquet** : rejeu d'un fichier, utile pour l'évaluation et la contre-expertise, pas une source d'exploitation.
- **Trous de collecte** : traités explicitement (créneau manquant, incident `STALE` au-delà de `stale_after`), mais une source qui renvoie des
  valeurs *plausibles et fausses* (compteur figé, collecteur qui répète la dernière valeur) n'est pas détectée.

### 2.3 Alertes sortantes

- **Garantie « au moins une fois »** : une coupure entre l'envoi et son enregistrement peut doubler un message (le webhook porte un
  identifiant de livraison pour dédupliquer ; Telegram et SMS n'en ont pas).
- **Aucun accusé de réception, aucune escalade, aucune astreinte** : si personne ne réagit, rien ne se passe de plus.
- **Telegram** : le bot doit être membre du groupe et l'identifiant du groupe (négatif) correct : non vérifié ici. Plafond par défaut
  15 messages/minute (limite annoncée : ~20 par minute et par groupe).
- **SMS** : un seul segment de 160 caractères, **sans accents** par construction. La passerelle Android appelle `POST /messages`
  (documentation actuelle) ; d'anciennes versions utilisent `/message` avec un autre corps : **chemin non configurable**, à vérifier
  sur la version installée. L'API Orange exige un forfait SMS acheté ; sa disponibilité exacte pour le Cameroun n'a pas été vérifiée au-delà
  de la documentation ; un SMS de groupe y est un appel par numéro (coût proportionnel).
- **Le texte des messages est en français**, fixe, non configurable.
- **Secrets** dans l'environnement ; aucun gestionnaire de secrets, pas de rotation. Un secret manquant **refuse** le démarrage.

### 2.4 Rapport de disponibilité

- **Définition** : indisponibilité = incident de sévérité critique (réglable via `downtime_severity`) ; dégradation = les autres incidents
  confirmés. C'est une convention du projet, pas celle d'un régulateur.
- **Période observée** = depuis la **première mesure conservée** (au plus tôt le début demandé). Les mesures brutes sont purgées au-delà de
  `history.preferred` et **le rapport ne lit pas l'archive Parquet** : un rapport sur 366 jours est donc tronqué à la rétention SQLite (40 jours
  en démonstration, 90 par défaut), et les incidents antérieurs sont exclus.
- **Pas de fenêtres de maintenance planifiée** à exclure, ni de PDF, ni d'envoi programmé.
- **Indicateurs du régulateur camerounais non vérifiés** : je n'ai pas trouvé la liste exacte de ce que l'ART exige ; le rapport actuel n'est pas
  un rapport réglementaire.

### 2.5 Sécurité et accès

- **Un seul niveau de droits** : tout utilisateur connecté peut tout modifier (`PUT /config`, démarrage/arrêt, envois d'essai). Pas de SSO/LDAP,
  pas de révocation d'une session avant son expiration, pas de renouvellement.
- **Limiteur de connexions en mémoire** (réinitialisé au redémarrage) ; derrière un proxy, il voit une seule adresse tant qu'uvicorn n'est pas lancé
  avec `--proxy-headers --forwarded-allow-ips` (non fait dans l'image).
- **`NM_ENV` vaut `development` par défaut** : `/docs` et `/openapi.json` sont alors exposés. L'image Docker fixe `production` ; un lancement
  manuel doit le faire.
- **Les endpoints de lecture sont ouverts** (sorties, historique, incidents, alertes, rapports, configuration sans secrets). Acceptable derrière un
  réseau d'exploitation ; à ne pas exposer tel quel sur Internet.
- **Pas de TLS** dans le projet : il revient à un proxy ou à un tunnel (§4).
- **Pas d'audit des modifications** au-delà d'une ligne de journal (qui a changé la configuration, quand).

### 2.6 Stockage et exploitation

- **SQLite, un seul écrivain, un seul processus** (verrou d'instance). Pas de haute disponibilité ; pas de réplication.
- **Pas de sauvegarde automatique**, **pas de migrations** (le schéma est créé au démarrage ; supprimer la base après une évolution du
  modèle). Acceptable tant qu'aucune donnée n'est à conserver ; **bloquant dès la première donnée réelle**.
- **Passage à l'échelle mesuré jusqu'à 90 sorties** (Mock, Linux) : calibration 9,8 s, 275 Mo de pic, tick 130 ms. Au-delà, c'est une
  extrapolation, pas une mesure. Le calcul est négligeable ; l'inconnue est la RAM d'une VM gratuite et le débit d'écriture SQLite.
- **Pas de `/metrics`** ni de signal externe si la boucle s'arrête : un monitoring qui tombe sans prévenir est le pire cas.
- **Pas de CI** : aucune vérification automatique ne tourne à chaque modification (§4).
- **Pas de journalisation structurée** ; les journaux sont du texte.

### 2.7 Frontend

- **Le tableau comparatif** émet 8 requêtes par sortie à chaque rafraîchissement de 15 s (4 fenêtres × valeur courante et précédente) : 24 pour
  3 sorties, **240 pour 30 sorties (calcul, non mesuré)**, faute d'un point d'accès « valeur la plus proche d'un instant ». Le cycle global mesuré
  (1,7 Ko) grâce à gzip/ETag cache le nombre de requêtes, pas leur coût côté serveur. C'est ce qui limitera l'interface avant la boucle.
- **Un seul navigateur testé** (Chromium). Ni Firefox, ni Safari, ni mobile réel (seulement une fenêtre de 375 px dans Chromium).
- **Pas de test visuel de non-régression** (captures de référence).
- **Textes en français uniquement**, non externalisés.
- **Pas de mode clair**, pas de réglage de taille de texte au-delà du zoom du navigateur.

### 2.8 Maintenance, cause amont, verdicts, supervision

- Une fenêtre de maintenance créée **pendant** un incident déjà annoncé ne le retient pas : seules les alertes émises ensuite le sont.
- La topologie est **statique et déclarée à la main** (`topology.parents`, un seul parent par sortie) : rien n'est déduit des données, et une cause commune à plusieurs équipements n'est pas modélisée.
- Un verdict n'expire pas et il n'y en a qu'un par incident (le dernier gagne, sans historique). Seul un incident **confirmé** se juge ; un signal faible jamais confirmé n'entre pas dans la précision.
- La précision mesurée n'est une précision que **sur les incidents jugés** : avec peu de verdicts, le pourcentage est fragile (la part jugée est affichée à côté). L'interface la calcule sur 30 jours fixes.
- La maintenance se compare à l'horloge **du serveur** (jamais à celle du navigateur), ≤ 14 jours par fenêtre.
- Le re-balayage `--from-db` rejoue exactement les **seuils** ; les **types** d'incident ne sont fiables que si les mesures brutes sont encore en base (purge au-delà de `history.preferred`). Seules les sorties ayant eu un incident confirmé dans la fenêtre sont rejouées : un réglage plus bas qui ouvrirait un incident sur une sortie restée silencieuse n'est pas vu. Maintenance et inhibition ne sont pas rejouées (elles ne changent pas l'ouverture, seulement l'envoi). `--end` est à donner pour une base à horloge simulée.
- `/health/ready` dit que la boucle travaille, **pas que les alertes arrivent** (les canaux n'ont jamais été éprouvés, §2.3). `/metrics` est ouvert tant que `NM_METRICS_TOKEN` n'est pas défini.
- Le signal de vie repose sur un service tiers et sur la joignabilité d'Internet depuis le serveur. Arrêter volontairement la surveillance l'envoie en échec (voulu). Le `HEALTHCHECK` Docker reste sur `/health` (liveness), pas sur `/health/ready`.
- La règle « boucle bloquée » des règles d'exemple suppose une période de pas égale à l'intervalle des mesures (source live) ; en démonstration (un pas toutes les 2 s pour 5 min simulées), elle est très tolérante.
- Le rapport de disponibilité du dashboard se rafraîchit toutes les 60 s : un verdict n'y apparaît pas instantanément (il apparaît tout de suite dans la ligne de précision).

## 3. Fonctionnalités proposées mais non réalisées

**Réalisées depuis** (voir §1 et §2.8) : fenêtres de maintenance et inhibition amont/aval, retour « faux/vrai positif » et re-balayage des seuils, `/metrics` et signal de vie externe. Les numéros ci-dessous sont conservés.

Chaque ligne : le besoin, d'où il vient, l'effort estimé (S ≈ 1 jour, M ≈ 2–4 jours, L > 4 jours) et comment savoir que c'est réussi.
Les recherches datent du 1er octobre 2026 et **sont à refaire avant de s'y fier** (URLs non conservées).

| # | Fonctionnalité | Besoin et source | Effort | Critère de réussite |
|---|---|---|---|---|
| 2 | **Planification de capacité** | Pratique d'opérateurs relevée en recherche : lancer la procédure d'augmentation à 50 % d'utilisation, l'achever à 70 % (95e centile d'échantillons de 5 min), en tenant compte du délai de réalisation. Demande la capacité de chaque lien (non disponible aujourd'hui) et les prévisions longues. | M | « Jours avant 70 % » par lien, avec un intervalle, validé sur un export réel. |
| 5 | **Connecteur Zabbix** | Zabbix stocke son historique en base (`history_uint` : `itemid`, `clock`, `value`, `ns`), lisible en SQL. Non écrit : schéma connu par la documentation, jamais essayé ; il faut gérer compteurs → débit (remises à zéro, bouclage) ou valeurs déjà exprimées en débit selon le gabarit. | M | Contrat de source vérifié sur une vraie base Zabbix. |
| 6 | **Rapports** : PDF, envoi programmé, exclusion des maintenances, indicateurs réglementaires | Le ministère camerounais a demandé des outils d'évaluation automatisés et le remboursement automatique du crédit data en cas de panne, ce qui exige des débuts et fins précis (déjà rétro-datés). Amende globale de 2,6 milliards FCFA en juillet 2025 (source de presse). **Indicateurs exacts de l'ART non trouvés.** | M | Rapport mensuel conforme à la liste exacte des indicateurs, obtenue auprès de l'ART. |
| 7 | **Escalade et acquittement** | Une alerte sans réponse doit monter (puis appeler quelqu'un d'autre) ; un opérateur doit pouvoir dire « je m'en occupe ». Une enquête d'éditeur (à prendre avec prudence) indique que 57 % des équipes d'astreinte jugent moins de 30 % de leurs alertes exploitables : l'acquittement et le regroupement réduisent le bruit. | M | Un incident non acquitté en 15 min relance un second groupe. |
| 8 | **Rôles** (lecture, opérateur, administrateur) et **SSO** | Aujourd'hui tout utilisateur connecté peut tout faire. | M–L | Un opérateur ne peut pas modifier les seuils ; connexion par l'annuaire de l'entreprise. |
| 9 | **Corrélation entre sorties** (topologie) | L'inhibition amont/aval existe (topologie déclarée à la main) ; il reste, plus largement, remonter à la cause (liens partageant un équipement ou une alimentation). Les coupures d'électricité et de fibre sont citées par le régulateur comme principaux facteurs de disponibilité. | L | Un scénario de panne d'alimentation multi-sorties produit un seul incident « cause commune ». |
| 10 | **WhatsApp** | Très utilisé dans la région, mais l'API entreprise n'est pas gratuite et n'a pas été évaluée. | ? | — |
| 11 | **Endpoint « valeur la plus proche d'un instant »** | Supprime la plus grosse partie des requêtes du tableau comparatif (§2.7). | S | 24 requêtes → 1 par rafraîchissement. |
| 12 | **Messages d'alerte configurables et multilingues** | Aujourd'hui fixes, en français. | S | Modèles de messages dans la configuration. |

## 4. Décisions reportées

### 4.1 Plan de déploiement en production (⏸ reporté par le propriétaire du projet)

Recherches du 1er octobre 2026 — **les offres gratuites changent souvent ; vérifier chaque ligne avant de s'engager**.

**Contraintes qui décident du choix**

- **La boucle doit tourner 24 h/24** : cela exclut les paliers qui s'endorment. Render gratuit : 750 h/mois, 512 Mo, sans carte, mais le service
  s'endort après 15 min d'inactivité (réveil d'environ une minute). Cloud Run : 180 000 vCPU-secondes par mois gratuites et retour à zéro, carte
  exigée. Fly.io a supprimé son palier gratuit en 2024. Koyeb : deux sources se contredisent (un service gratuit avec carte et blocage de 29 $,
  ou seulement une base Postgres gratuite) : à vérifier sur place.
- **Cartes bancaires** : Oracle exige un téléphone et une carte (non débitée), l'approbation dépend du pays et de la capacité de la région. Sans
  carte internationale, seuls Render et Cloudflare restent accessibles.
- **Les « toujours gratuit » bougent** : Oracle a réduit l'allocation ARM de 4 OCPU / 24 Go à 2 OCPU / 12 Go, limite appliquée depuis le
  18 août 2026.
- **Télémétrie réelle** : elle ne devrait pas partir vers un cloud public gratuit. L'exploitation réelle se fait sur une machine du réseau
  d'exploitation (là où Zabbix, Prometheus ou la base de mesures sont déjà joignables).
- **Coupures d'électricité** : premier facteur de disponibilité cité par le régulateur ; elles s'appliquent aussi à la machine qui héberge le moniteur.

**Piste A — démo publique (portfolio, données simulées, 0 €)** : interface sur Cloudflare Pages (bande passante statique illimitée, 500 builds par
mois) ; API sur Render gratuit (le processus pèse ~222 Mo mesurés pour 3 sorties, il tient dans 512 Mo ; `NM_ENV=production`, un worker).
Limites : la démo « gèle » quand le service dort, la base est éphémère. Avec une carte : Oracle A1 (2 OCPU / 12 Go, toujours allumé) ; l'e2-micro de
Google Cloud est limité à trois régions américaines, 30 Go de disque et **1 Go de sortie par mois**. Avant le correctif de trafic (66 Ko par cycle
de rafraîchissement), un seul onglet l'aurait épuisé en quelques heures ; à 1,7 Ko par cycle de 4 à 5 s, **un seul onglet laissé ouvert en
permanence représente environ 1 Go par mois (estimation par calcul, non mesurée sur la durée)**, soit tout le quota : insuffisant dès que
plusieurs personnes consultent. L'onglet masqué ne coûte rien (polling suspendu).
**Cette démo doit afficher le bandeau « Données simulées »** (déjà fait) pour ne pas être prise pour un vrai réseau.

**Piste B — production réelle (0 € hors machine)** : `docker compose` avec l'API (un worker) ; TLS et gzip par un proxy (Caddy) ou un tunnel ;
SQLite répliquée en continu par Litestream vers Cloudflare R2 (gratuit jusqu'à 10 Go, 1 million d'opérations d'écriture et 10 millions de lecture par mois,
sortie gratuite) — **nombre d'écritures de Litestream non vérifié** ; accès sans IP publique par Cloudflare Tunnel (gratuit, connexion sortante,
aucun port ouvert) ; surveillance du moniteur par Healthchecks.io (§3, point 4) ; alertes par Telegram, SMS (passerelle Android avec une SIM locale) et
webhook (déjà là).

**Ordre conseillé**

1. Valider les trois canaux d'alerte avec un vrai message d'essai (`POST /notifications/test/{canal}`) — une heure.
2. Construire l'image chez le propriétaire et corriger ce qui casse — une demi-journée.
3. Démo publique (piste A).
4. Piste B sur une VM de test, avec une source en lecture seule.
5. Marche à blanc de 2 à 4 semaines, puis re-balayage des seuils avec `python -m network_monitor.analysis.evaluation --sweep` (§5).

### 4.2 Phase 6 : industrialisation (⛔ exclue à la demande du propriétaire, sauf Docker)

- **CI** (GitHub Actions) : **écrite** (`.github/workflows/ci.yml`, ❓ jamais exécutée) : backend + MariaDB, frontend, e2e, image Docker + test de fumée, `pip-audit`.
  Les minutes gratuites pour ce dépôt n'ont pas été vérifiées. Le Prometheus réel est omis (node_exporter scruté toutes les 5 s, trop fragile sur un exécuteur partagé).
- **Matrice Windows** : un job Windows **informatif** (`continue-on-error`) lance les tests ; le propriétaire développe sous Windows et le verrou d'instance a une branche `msvcrt`
  jamais exécutée (`pragma: no cover`). À rendre bloquant seulement quand il passe et avant de promettre le support de Windows.
- **Fichiers de projet** volontairement écartés pour ne pas multiplier les fichiers : `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`,
  `.editorconfig`, `pre-commit`, Dependabot. À reconsidérer quand il y aura d'autres contributeurs.
- **Scan d'images et de dépendances** : `pip-audit` informatif en CI (jamais exécuté) ; Trivy non fait. `npm audit` ne signale rien aujourd'hui (0 vulnérabilité).

### 4.3 Autres reports

- **Migrations (Alembic)** : écartées tant que la base est jetable ; à introduire avant la première donnée réelle (§2.6).
- **PostgreSQL / TimescaleDB** : écartés ; SQLite suffit à l'échelle mesurée. À reconsidérer au-delà de quelques centaines de sorties ou pour de la haute disponibilité.
- **Connecteur SNMP direct / gNMI** : écarté au profit de Prometheus, qui les agrège déjà.
- **TypeScript strict de bout en bout** : fait pour tout le frontend ; les fichiers de configuration (`vite.config.js`) restent en JavaScript.
- **Test visuel de non-régression** et **autres navigateurs** : reportés.

## 5. Prochaines étapes, avec critère de sortie

| Étape | Critère de sortie |
|---|---|
| 1. Valider les alertes en vrai | Un message reçu sur chaque canal voulu, le jeton absent des journaux ; un SMS de groupe reçu par tous les numéros. |
| 2. Construire l'image | `docker compose up` : `/health` répond, `HEALTHCHECK` passe, une seconde instance est refusée (le job `docker` de la CI le vérifie, une fois exécuté). |
| 3. Faire passer la CI au vert | Premier `push` : les six jobs s'exécutent ; corriger ce qu'ils révèlent (Windows et Docker n'ont jamais tourné). |
| 4. Brancher une vraie source en lecture seule | Prometheus ou SQL réel ; requêtes PromQL validées lien par lien ; `source.type` ≠ `mock` (le bandeau disparaît). |
| 5. Marche à blanc de 2 à 4 semaines | Aucune alerte envoyée au début ; incidents examinés à la main ; faux positifs comptés (les verdicts du dashboard y servent). |
| 6. Re-balayer les seuils sur les données réelles | `--sweep` sur les captures réelles ; défauts de `IncidentConfig` mis à jour avec la nouvelle mesure. |
| 7. Mettre en place la sauvegarde et les migrations | Restauration d'une sauvegarde testée ; première migration Alembic. |
| 8. Déploiement (§4.1) | Piste A en ligne ; piste B sur une VM, avec signal de vie externe. |
| 9. Fonctionnalités du §3, dans l'ordre 7, 6 | Selon l'usage constaté pendant la marche à blanc. |

## 6. Dette technique et points d'attention du code

- `state` est un singleton de module dans `app/api.py` (les tests le remplacent). Pratique, mais interdit toute instance multiple par processus.
- `DEFAULT_API_CONFIG` est la **configuration de démonstration** (Mock, 40 jours d'historique) ; elle ne doit jamais être celle d'une exploitation.
- La table `alerts` garde **tous** les événements (y compris signaux faibles) pour l'audit ; `GET /alerts` ne rend que les alertes notables par défaut.
- Les seuils de motif d'incident (`_PATTERN_*`, `_MIN_*`) sont des constantes de module, pas de la configuration.
- La boucle de monitoring est un thread ; un calcul très long (calibration à beaucoup de sorties) bloque les ticks pendant sa durée.
- `MonitoringLoop` lit `AppConfig` directement : `PUT /config` ne modifie que les seuils d'incident et le seuil critique, jamais la source ni les canaux
  (ils exigent un redémarrage).
- Le journal ne distingue pas les niveaux d'importance d'un déploiement (pas de corrélation d'identifiant de requête).

## 7. Défauts réels trouvés par la validation (pour mémoire)

La validation dans un vrai navigateur et contre de vrais services a révélé des défauts que les tests unitaires ne voyaient pas : libellés d'axe
tronqués (marge trop étroite), « maintenant » recouvrant un libellé, bouton imbriqué dans un élément « bouton » (axe : sérieux), contrastes de texte
insuffisants (4,07:1 et 2,27:1), requête vers Google Fonts (privacy et dépendance à Internet), **disponibilité de 99,456 % affichée pour un système
qui n'observait que depuis 25 minutes** (le rapport se calculait sur 30 jours), panneau d'alertes noyé de signaux faibles non confirmés, connexion
MariaDB morte non détectée (`pool_pre_ping` manquant), Isolation Forest aveugle aux paliers. Chacun est corrigé et gardé par un test.
Les sources consultées pour le §3 et le §4.1 : documentation Oracle Cloud (offre gratuite), Render, Google Cloud (niveau gratuit), Cloudflare (Pages, R2,
Tunnel), Healthchecks.io, Prometheus Alertmanager, API Bot de Telegram, Orange Developer (API SMS), SMS Gateway for Android, documentation Zabbix,
presse sur l'ART Cameroun.
