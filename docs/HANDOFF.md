# Daily Guessr - état de la migration

Dernière mise à jour : 29 juin 2026.

## Objectifs

- Remplacer l'ancienne marque par `Daily Guessr`.
- Renommer le mode auteur visible en `Qui a écrit ça ?`.
- Publier l'Activity dans le dépôt public sans donnée privée.
- Héberger `TERMS.md` et `PRIVACY.md` dans le dépôt GitHub public.
- Afficher le classement à gauche, le jeu au centre et la progression live des
  participants à droite.
- Garantir que le suivi des quatre modes ne révèle aucun résultat avant que
  l'observateur ait terminé le mode concerné.

## Règles de confidentialité

Ne jamais copier vers le dépôt public :

- `.env`, base SQLite, journaux, alias locaux ou médias privés ;
- domaine, adresse IP, chemins ou identifiants du déploiement réel ;
- identifiants Discord de production ou de test ;
- noms, pseudonymes, avatars, messages ou médias réels des membres ;
- nom civil, adresse e-mail ou autre donnée personnelle du propriétaire.

Tous les exemples publics doivent utiliser des valeurs manifestement fictives.

## Avancement

- [x] Audit initial des deux dépôts.
- [x] Détection d'un nom civil dans l'historique du dépôt public.
- [x] Choix de la marque `Daily Guessr`.
- [x] Migration de la marque dans le code et les ressources du dépôt privé.
- [x] Suppression des pages juridiques servies par le domaine privé.
- [x] Création de `TERMS.md` et `PRIVACY.md` dans le dépôt public assaini.
- [x] Portage et assainissement de l'Activity dans le dépôt public.
- [x] Audit du contenu courant : aucun secret ou identifiant réel détecté.
- [x] Suivi live global aux quatre modes avec présence temporaire.
- [x] Anti-spoil personnalisé appliqué côté serveur.
- [x] Nouvelle mise en page responsive à trois zones.
- [x] Timer média Hardcore : image 25 s, GIF 40 s, vidéo durée + 25 s, plafond
  total de 2 min 30.
- [x] Grille du classement corrigée : rang, avatar, nom, points, ratio et streak
  restent désormais sur une seule ligne.
- [x] Rich Presence configurée avec les visuels `1` et `2`.
- [x] Lecteur média externe anti-spoil via le SDK Activity, sans lien vers le
  message Discord, téléchargement forcé ni transcodage serveur.
- [x] Premier état du suivi live injecté au rendu, sans attendre le SSE.
- [x] Tooltips live anti-spoil avec temps et guess pour les quatre modes.
- [x] Mode Remets dans l'ordre avec cinq messages, médias, score partiel de 0 à 5,
  streak dédiée et classement publié avec indicateur des joueurs du jour.
- [x] Emojis de streak enrichis par paliers de cinq, anciens symboles conservés.
- [x] Bouton persistant dans l'annonce quotidienne pour lancer l'Activity.
- [x] Lancement mobile fiabilisé : la coque Vite reste une SPA et charge `/daily`
  dans une iframe interne, sans navigation principale bloquée par Android.
- [x] Partage compact et réutilisable du bilan emoji après les quatre modes dans le
  salon du ping quotidien.
- [x] Tests privés : 47 tests Python, build Vite et vérifications de syntaxe.
- [x] Dépôt public renommé en `Baach691/daily-guessr`.
- [x] Historique public remplacé par le commit racine assaini `fb66705`.
- [x] Feature live et correctifs suivants recopiés et assainis dans le dépôt public.
- [x] Nouveau mode et paliers d'emojis recopiés et assainis dans le dépôt public.
- [x] Tests, compilation, audit de confidentialité et build exécutés dans les deux
  dépôts.
- [x] Nouveau lot synchronisé et validé, prêt pour publication.

## Suivi live

- Broker : une clé `(guild_id, date)` commune aux quatre modes.
- Présence : heartbeat toutes les 15 s, expiration après 45 s.
- États : `win`, `fail`, `playing`, `complete` masqué, `waiting`.
- Avant la réponse du viewer : progression uniquement ; résultats et classement
  détaillés restent vides.
- Après sa réponse : révélation des résultats du mode courant et classement.
- Transport : SSE, snapshot de fraîcheur toutes les 15 s, polling 3 s en fallback.
- Tests : `tests/test_realtime_updates.py`.

## Publication publique

- Dépôt : `https://github.com/Baach691/daily-guessr`
- Conditions : `https://github.com/Baach691/daily-guessr/blob/main/TERMS.md`
- Confidentialité : `https://github.com/Baach691/daily-guessr/blob/main/PRIVACY.md`
- Le commit public est sans parent et utilise une identité générique.

Les fichiers non suivis présents localement dans les deux dépôts ne doivent pas être
ajoutés automatiquement. En particulier, les vidéos privées du dépôt privé et les
images locales non classées du dépôt public restent hors des commits.
