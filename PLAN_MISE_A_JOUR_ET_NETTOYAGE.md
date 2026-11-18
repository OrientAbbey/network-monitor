# Plan de mise à jour des dépendances et de nettoyage

**Dernière mise à jour :** 18 novembre 2026
**Branche de travail :** `chore/update-dependencies`
**Environnement requis :** `.venv` avec Python 3.12
**Statut global :** Validations Python et build frontend réussis ; artefacts régénérables nettoyés. L’audit npm complet signale encore quatre avis élevés transitifs de développement.

## Règles de travail

- Préserver les changements locaux déjà présents ; ne pas réinitialiser, écraser ou supprimer du travail utilisateur.
- Faire les opérations Python dans `.venv` et vérifier explicitement la version Python 3.12.
- Ne supprimer que des artefacts manifestement régénérables ou du code confirmé inutilisé ; consigner les suppressions.
- Garder les manifestes et fichiers de verrouillage synchronisés.
- Ne pas rétrograder une dépendance vers une version vulnérable pour faire disparaître un avis.
- Ne pas créer de dépôt GitHub (le dépôt existe déjà) ; commit et push autorisés explicitement par l'utilisateur le 18 novembre 2026.

## Suivi des tâches

Légende : `[x]` terminé et vérifié · `[ ]` à faire · `[!]` anomalie connue / action requise.

### 1. État initial et protection des changements

- [x] Confirmer la branche de travail `chore/update-dependencies`.
- [x] Repérer les changements locaux à préserver : README, manifestes/verrous de dépendances et corrections Python liées au stockage/tests.
- [x] Vérifier qu’aucun fichier `PLAN`/`TASK` existant n’est disponible pour ce suivi.
- [x] Réexaminer le diff des changements suivis (README, frontend, cycle de vie API/stockage et tests) avant toute suppression supplémentaire.
- [x] Réexaminer les fichiers non suivis `pyproject.toml`, `uv.lock` et ce plan dans le diff final ; `uv tree --locked` confirme les 37 paquets et leurs dépendances.
- [x] Confirmer que `.gitignore` est déjà suivi et couvre `.venv`, les caches, `*.egg-info`, les bases SQLite et les artefacts frontend.
- [x] Confirmer que `network_monitor.db` est ignorée ; la préserver car elle peut contenir des données d’exécution utiles.

### 2. Environnement Python 3.12

- [x] Vérifier que `.venv` utilise Python 3.12.10.
- [x] Vérifier `uv lock --check`.
- [x] Vérifier `pip check` depuis l’environnement du projet.
- [x] Réexécuter après les mises à jour : Python 3.12.10 confirmé, `uv lock --check` cohérent (37 paquets résolus) et `pip check` sans dépendance cassée.

### 3. Mise à jour des dépendances

Les mises à jour suivantes sont déjà présentes dans le travail local avant la création de ce plan ; elles restent à examiner dans le diff final. Le script d'esbuild a été inspecté et approuvé pour la validation locale, mais l'approbation n'est pas conservée dans le manifeste ni le lockfile ; un environnement neuf peut redemander cette approbation.

- [x] Déclarer/mettre à jour les dépendances Python dans `pyproject.toml`.
- [x] Générer le verrou Python `uv.lock` et exporter `requirements.txt`.
- [x] Mettre à jour `frontend/package.json` et son verrou `frontend/package-lock.json`.
- [x] Vérifier l’inventaire des imports Python : `numpy`, `statsmodels`, FastAPI, Uvicorn, Pydantic, SQLAlchemy et PyYAML sont utilisés ; `scipy` est une dépendance transitive de `statsmodels` et n’est pas importée directement.
- [x] Évaluer la dépendance directe `scipy` : la conserver pour respecter la déclaration existante et rendre explicite le socle numérique requis par les modèles ; elle est aussi requise transitivement par `statsmodels`.
- [x] Vérifier que `frontend/package-lock.json` existe avec `lockfileVersion: 3` et que ses dépendances racines correspondent à `frontend/package.json`.
- [x] Confirmer l'environnement Python installé depuis le verrou : versions directes conformes au lock (numpy 2.5.5, scipy 1.18.1, statsmodels 0.15.0, FastAPI 0.163.2, Uvicorn 0.54.0, Pydantic 2.14.3, SQLAlchemy 2.1.9, PyYAML 6.0.3, httpx 0.28.1, pytest 9.2.0).
- [x] `npm ci` depuis `frontend/` a réussi après une nouvelle exécution seule : 66 paquets ajoutés. Le premier essai a rencontré `ENOTEMPTY` dans `caniuse-lite/data/features`; ne jamais lancer deux installations npm en parallèle.
- [!] Avis npm élevé `GHSA-fc5q-39xc-vjpf` sur `picomatch` 4.0.7 dans l’arbre de développement ; quatre avis transitifs élevés signalés au total par l'audit complet. L'audit de production est propre, mais l'audit complet ne l'est pas. La version publiée par le registre consulté est encore 4.0.7 ; la version 4.0.8 n'y est pas disponible.

### 4. Nettoyage prudent du projet

- [x] Inspecter `.gitignore` : règles existantes couvrant Python, `.venv`, caches, `*.egg-info`, bases SQLite, `node_modules`, `frontend/dist` et fichiers d’environnement.
- [x] Confirmer qu’aucun fichier base de données ignoré ne sera supprimé sans confirmation, car il peut contenir des données utiles.
- [x] Vérifier la présence de caches et métadonnées au premier passage : `.pytest_cache` et `network_monitor.egg-info` étaient présents, les deux ignorés et régénérables.
- [x] Après la fin des tests, retirer les sept dossiers `__pycache__` sous `network_monitor/`, `.pytest_cache` et `network_monitor.egg-info` ; ne pas toucher aux caches de `.venv`.
- [x] Vérifier l'absence de ces artefacts après nettoyage. `network_monitor.db` (2 555 904 octets, datée du 28 septembre 2026) est conservée ; aucune source, configuration ni donnée utile n'a été supprimée.

### 5. Corrections et validation des tests

- [x] Corriger la fermeture des connexions SQLAlchemy/SQLite pour éviter les erreurs de suppression sous Windows.
- [x] Fournir une implémentation sans effet de `Repository.close()` pour préserver la compatibilité des autres backends éventuels.
- [x] Placer la calibration et le démarrage dans le bloc `try/finally` du lifespan pour que le stockage soit aussi fermé si l'initialisation échoue.
- [x] Rendre l’essai API du scénario injecté déterministe en avançant explicitement les ticks après l’arrêt du thread.
- [x] Vérifier isolément le test API du scénario injecté : 1 réussi après correction.
- [x] Vérifier les tests dépôt/API avant la dernière modification du test : 18 réussis.
- [x] Vérifier les tests de scénarios : 11 réussis.
- [!] Suites complètes précédentes : 63 réussis / 3 échecs (`time` manquant), puis 65 réussis / 1 échec intermittent dû à la dépendance au thread et au délai fixe.
- [x] Remplacer les délais fixes par un arrêt du thread et un tick manuel dans les tests API qui exigent des données persistées.
- [x] Suite API ciblée après correction : 13 réussis, 1 avertissement de dépréciation Starlette concernant `httpx`.
- [x] Suite complète `.venv\Scripts\python.exe -m pytest network_monitor/tests/ -q` : 66 réussis, aucun échec ; dernière exécution confirmée en 995,51 s (une exécution précédente : 977,03 s).
- [x] Aucun échec restant à corriger ; un avertissement de dépréciation Starlette concernant l'usage de `httpx` dans `starlette.testclient` subsiste.

### 6. Vérifications frontend, sécurité et diff final

- [x] `npm ci` depuis `frontend/` réussi (66 paquets) ; le script d'installation `esbuild@0.25.12` avait d'abord été bloqué, puis explicitement approuvé avec `npm install-scripts approve esbuild` après inspection du script de téléchargement/validation du binaire officiel. Cette approbation était locale à la configuration npm ; le manifeste et le lockfile n'ont pas été modifiés pour la rendre persistante. Un clone propre pourra nécessiter `npm install-scripts approve esbuild` après vérification du script.
- [x] Build après `npm ci` et approbation ciblée de `esbuild` : `npm run build` réussi avec Vite 6.4.6 (27 modules).
- [x] `npm audit --omit=dev` : 0 vulnérabilité de production (confirmé après `npm ci`).
- [!] Audit npm complet : 4 avis élevés transitifs liés à `picomatch`/`fdir`/`tinyglobby`/Vite, dont `GHSA-fc5q-39xc-vjpf` (`picomatch <=4.0.7`, ReDoS). npm propose Vite 6.2.7 comme correctif, ce qui est une rétrogradation de la version actuelle 6.4.6 ; ne pas appliquer sans évaluer sa sécurité et sa compatibilité.
- [x] Vérifier l'égalité entre la racine de `frontend/package.json` et celle du lockfile ; `git diff --check` réussi lors du dernier contrôle.
- [x] Build et audits npm relancés après `npm ci`; le build passe et l'audit production signale zéro vulnérabilité. L'audit complet confirme 4 avis élevés transitifs pour `picomatch` (ReDoS), relayés via `fdir`/`tinyglobby`/Vite ; pas de `npm audit fix --force` appliqué ni de rétrogradation de Vite.
- [x] Corriger les constats périmés du README : la revue du 28 septembre et les anciens résultats de tests ne sont plus présentés comme l’état courant.
- [x] Séparer dans le README les commandes de venv Windows PowerShell et macOS/Linux pour éviter une copie accidentelle de commandes incompatibles.
- [x] Après la suite complète, nettoyer les caches du projet autorisés, confirmer l’état Git final et préserver `network_monitor.db`.
- [x] `git diff --check` final réussi ; aucun commit ni push effectué.
- [x] Relecture indépendante des changements non commités : aucun constat concret ; `network_monitor.db` est restée intacte.

## Critères de clôture

- Python 3.12 est confirmé dans `.venv` ; synchronisation et cohérence du verrou Python validées.
- Installation/verrou frontend cohérents et build réussi.
- Suite Python complète passée (66 tests réussis) ; avertissement Starlette documenté.
- Nettoyage limité à des éléments sûrs et régénérables, avec bilan explicite.
- Les avis npm non corrigés sont signalés ; aucun audit complet n’est présenté comme propre tant qu’ils subsistent.
- Diff final relu ; aucun commit ni push sans autorisation.
