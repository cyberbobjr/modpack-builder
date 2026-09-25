# Créateur de modpacks Project Zomboid

Cette interface Python permet de choisir des mods installés dans le Workshop de Project Zomboid, puis de les copier dans un projet Workshop local indépendant. Chaque copie reçoit un `modId` préfixé et reste un mod distinct sous `Contents/mods`. L'outil cible actuellement la Build **42.20.4** et génère le mod principal dans `42.20` ; il n'a pas été validé en 42.21.

## Démarrage

Prérequis : Python 3.10 ou plus récent et les mods source installés via le Workshop Steam. Leur dossier se choisit dans l'onglet **Paramètres**.

Dans PowerShell, depuis `E:\modpack-builder` :

```powershell
Set-Location 'E:\modpack-builder'
python -m pip install --target .batman_modpack_deps -r requirements-batman-modpack.txt
python run_batman_modpack.py
```

Ouvrir ensuite <http://127.0.0.1:8501>. Garder le terminal ouvert pendant l'utilisation. Le lanceur emploie Streamlit installé dans `.batman_modpack_deps` et écoute uniquement sur l'ordinateur local.

## Paramètres et recherche des mods

Dans **Paramètres**, saisir le chemin du dossier `steamapps/workshop/content/108600`, puis cliquer sur **Enregistrer le dossier**. Choisir le dossier qui contient les sous-dossiers numériques Workshop, pas le dossier d'un seul mod.

Le bouton **Détecter les bibliothèques Steam** recherche les installations locales usuelles et les bibliothèques déclarées par Steam. Sélectionner un résultat, puis **Utiliser ce dossier**. Si nécessaire, ouvrir **Rechercher dans un autre emplacement** pour scanner un dossier ou un disque précis. Cette recherche s'arrête après 20 000 dossiers et signale les accès refusés ; les liens et certains dossiers système sont ignorés.

Le chemin est conservé entre les lancements dans `.modpack-builder-settings.json`, exclu de Git. Un changement de dossier réinitialise la sélection et la vérification, recharge le catalogue et met à jour le chemin proposé dans **Mises à jour**. Les mods source ne sont pas modifiés.

## Créer un pack

1. Ouvrir **Projet et identifiants**. Le dossier parent proposé est `C:\Users\<utilisateur>\Zomboid\Workshop`. Donner un nom de dossier différent à chaque pack ; le préfixe des copies et le `modId` principal sont proposés à partir de ce nom et restent modifiables.
2. Rechercher un mod par **nom**, **modId** ou **ID Workshop**, puis cocher **Ajouter**. Le filtre se met à jour pendant la saisie. Le `modId` est un lien vers la page Steam Workshop du mod, ouverte dans un nouvel onglet ; un premier clic peut sélectionner la cellule du tableau. Les dépendances déclarées avec `require=` sont cochées récursivement. On peut aussi importer les favoris ou une liste nommée depuis `C:\Users\<utilisateur>\Zomboid\Lua\pz_modlist_settings.cfg` ; le fichier est seulement lu. Les IDs absents ou ambigus sont signalés.
3. Examiner les colonnes **Incompatibles déclarés** et **Conflit avec sélection**. L'interface repère les paires déclarées incompatibles dans le `mod.info` actif, y compris lorsqu'un seul des deux mods déclare le conflit.
4. Cliquer sur **Vérifier la sélection**. La barre de progression indique le nombre de mods dont les fichiers ont été analysés ; une sélection de plusieurs centaines de mods peut prendre du temps. La vérification signale les dépendances manquantes, les incompatibilités déclarées, les collisions d'IDs et les chemins `media` communs. Examiner les avertissements sur les références internes avant de confirmer leur prise en compte.
5. Cliquer sur **Ajouter les mods cochés au pack**. Pour compléter le même pack plus tard, conserver son dossier, son préfixe et son `modId` principal ; les composants déjà inscrits dans le manifeste sont indiqués dans la liste.

Pendant l'ajout, un état de lancement et une barre de progression indiquent la vérification, la préparation temporaire, la copie vers le pack et le contrôle final. Les fichiers arrivent dans la destination après la préparation temporaire. Un message confirme le succès avec le chemin du pack ou indique l'erreur rencontrée. Si le bouton est désactivé, un message précise l'action nécessaire.

Le bouton **Actualiser** relit les mods Workshop après une mise à jour de leurs fichiers.

Les copies affichent `[NomDuPack] Nom du mod` dans le jeu, où `NomDuPack` est le nom du dossier du pack. Les noms des originaux restent inchangés. Pour un pack déjà généré, utiliser **Préfixer les noms des copies déjà présentes** dans l'onglet **Création du pack**. Ce bouton sauvegarde les anciens fichiers sous `mod.info.before-name-prefix.bak`, conserve les IDs et le manifeste, et n'ajoute pas deux fois le même préfixe.

## Mises à jour des mods

L'onglet **Mises à jour** compare les fichiers locaux à un commit Git. Git doit être installé et accessible dans le terminal. Le dossier proposé est celui des sources Workshop ; il peut être remplacé par la racine d'un autre dépôt de mods.

1. Si le dossier ne contient pas de dépôt, cliquer sur **Initialiser Git dans ce dossier**. Aucun fichier de mod n'est modifié.
2. Sans historique, cliquer sur **Enregistrer le premier état de référence**. Les fichiers non ignorés sont enregistrés dans un commit local. Cette opération peut prendre du temps et de l'espace disque. Elle ne permet pas de retrouver les versions antérieures.
3. Choisir un commit parmi les 50 derniers, puis cliquer sur **Rechercher les changements**. Le tableau regroupe par mod les fichiers ajoutés, modifiés, supprimés et non suivis, y compris les changements non commités. Les fichiers ignorés non suivis sont exclus.
4. Filtrer les mods à examiner, choisir un fichier et cliquer sur **Afficher les différences**. Les aperçus longs sont tronqués ; les renommages apparaissent comme une suppression et un ajout.

Steam doit avoir téléchargé les mises à jour avant la comparaison. Cette première version sert à examiner les changements : elle ne remplace pas les mods du pack, ne suit pas encore leur révision source dans le manifeste et ne publie rien sur GitHub. Un dépôt existant conserve son historique et son index lors de la consultation. Les futurs commits de référence restent à créer depuis Git.

Les tests du suivi et des paramètres utilisent des dossiers temporaires : `python -m unittest test_mod_updates test_settings -v`.

## Structure générée

Avec le dossier par défaut `modpack-42-20`, le résultat ressemble à ceci :

```text
C:\Users\<utilisateur>\Zomboid\Workshop\modpack-42-20\
├── workshop.txt
├── preview.png
└── Contents\mods\
    ├── modpack_42_20_pack\
    │   ├── common\
    │   └── 42.20\
    │       ├── mod.info
    │       ├── batman-modpack-selection.json
    │       └── mod.info.before-modpack-builder.bak
    └── modpack_42_20_<WorkshopID>_<nom-du-mod>\
        ├── common\
        └── 42...\
```

Chaque composant copié possède son propre `mod.info`, à la racine, dans `common` ou dans une variante de version selon le mod source. Le générateur ajoute `common` s'il manque et place les mods source sans variante dans `42.20`. Il réécrit les `id=` et les relations déclarées entre mods copiés (`require`, `loadModAfter`, `loadModBefore`, `incompatible`). Le manifeste enregistre les sources et les IDs générés ; la sauvegarde du `mod.info` principal permet de vérifier les ajouts ultérieurs. Cette disposition suit la [structure des mods Project Zomboid](https://pzwiki.net/wiki/Mod_structure).

## Limites

Le préfixage ne réécrit pas les références dans le Lua, les noms d'objets, les packs de textures, les tuiles ni les données déjà enregistrées dans une sauvegarde. Une incompatibilité non déclarée par les auteurs peut donc subsister. Les collisions de chemins `media` sont signalées comme avertissements, pas corrigées automatiquement. Désactiver les mods originaux lors du test du pack et vérifier un démarrage complet ainsi que les actions concernées dans le jeu, de préférence sur une copie de sauvegarde. Aucune validation en jeu ou en 42.21 n'est implicite.

Le `workshop.txt` et l'image générés servent de point de départ local. Avant une publication sur Steam Workshop, renseigner les métadonnées et remplacer l'image provisoire.

## Changelog

### 2026-09-25

- Noms des copies préfixés avec `[NomDuPack]` et bouton pour appliquer le préfixe aux copies existantes avec sauvegarde.
- Paramètres persistants pour choisir le dossier source et rechercher les bibliothèques Workshop.
- Onglet de comparaison Git des mods, avec initialisation facultative et premier état de référence.
- Progression de génération et messages de lancement, de succès et d'erreur.

L'historique détaillé est disponible dans [CHANGELOG.md](CHANGELOG.md).
