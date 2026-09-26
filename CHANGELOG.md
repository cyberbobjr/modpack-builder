# Changelog

## 2026-09-26

### Détection des conflits

- Nouvel onglet **Conflits** : analyse en lecture seule des composants du pack et des mods cochés, ou d'un choix manuel dans le catalogue. Le choix manuel utilise le même tableau que la création du pack (recherche, cases à cocher, filtre des mods cochés), avec des boutons pour cocher la sélection du pack ou tout décocher.
- Fonctions Lua globales redéfinies par plusieurs mods, avec distinction entre remplacement sans appel de l'original (gravité élevée) et chaînage (gravité faible).
- Fichiers `media/lua` fournis par plusieurs mods, dont seul le dernier chargé est exécuté.
- Objets, recettes `craftRecipe` et véhicules définis plusieurs fois dans les scripts.
- Seuls les dossiers chargés pour 42.20.4 sont analysés (`common` et variante retenue). Les ajouts à des événements ne sont pas signalés.
- Analyse indicative, fondée sur des motifs textuels : elle ne modifie aucun fichier et ne remplace pas un test en jeu.

## 2026-09-25

### Noms affichés des copies

- Les copies générées affichent `[NomDuPack] Nom du mod`. Le préfixe reprend le nom du dossier du pack et s'applique aux fichiers `mod.info` de toutes les variantes copiées.
- Les ajouts à un pack existant utilisent le même format.
- Le bouton **Préfixer les noms des copies déjà présentes** permet de mettre à jour les anciens packs. Chaque fichier modifié est sauvegardé dans `mod.info.before-name-prefix.bak`. Une nouvelle utilisation du bouton ne répète pas le préfixe.
- Les sources, les IDs, les dépendances, le manifeste et la sauvegarde du mod principal sont conservés par cette mise à jour des noms.
- Le préfixe sert à distinguer les copies dans la liste. Les originaux doivent toujours être désactivés pour la partie qui utilise le pack.

### Paramètres

- Choix du dossier Workshop depuis l'interface et sauvegarde locale du chemin, exclue de Git.
- Détection des bibliothèques Steam et recherche dans un emplacement choisi.
- Actualisation du catalogue et du chemin proposé pour le suivi Git après un changement de dossier source.

### Suivi des mises à jour

- Onglet **Mises à jour** : comparaison avec un commit Git, regroupement par mod et aperçu des différences.
- Proposition d'initialisation Git et d'enregistrement d'un premier état de référence.
- Prise en compte des ajouts, suppressions et modifications locales, y compris non commitées. Cette version ne remplace pas les composants du pack.

### Génération

- Affichage du lancement, des étapes de vérification et de copie, puis du résultat ou de l'erreur.
- Messages expliquant pourquoi l'ajout est indisponible.
- Création du projet : catalogue Workshop, sélection des dépendances, analyse des conflits, copie des mods et réécriture des IDs pour la cible 42.20.4.

Les contrôles automatisés utilisent des mods fictifs et des dossiers temporaires. La compatibilité en jeu reste à valider séparément.
