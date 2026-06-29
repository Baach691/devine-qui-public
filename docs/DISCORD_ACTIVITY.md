# Daily Guessr dans Discord

> État : Activity jouable avec les trois modes.
>
> Ce document décrit l'architecture actuellement utilisée et la procédure de
> déploiement. Il ne contient aucun identifiant ni domaine de production.

## Vue d'ensemble

```text
Commande /daily
      |
      v
Discord Activity (iframe via discordsays.com)
      |
      v
Backend Flask HTTPS
  |- build Vite de activity/
  |- échange OAuth2 /api/token
  |- session signée /api/activity/session
  |- page et API /daily/*
      |
      v
SQLite commune au bot et au site
```

La version web classique et l'Activity utilisent la même page de jeu, les mêmes
contrôles serveur et la même base. L'Activity est un canal d'accès supplémentaire,
pas une seconde implémentation du moteur.

## Authentification

1. Le frontend Vite initialise `DiscordSDK`.
2. Il demande un code OAuth2 avec `authorize`.
3. `POST /.proxy/api/token` échange ce code côté serveur.
4. Le frontend appelle `authenticate` avec l'access token.
5. `POST /.proxy/api/activity/session` vérifie :
   - l'application OAuth ;
   - l'identité Discord ;
   - l'appartenance au serveur ;
   - la whitelist de rôles éventuelle.
6. Le backend renvoie une URL `/daily?t=<token>` signée pour ce joueur, ce serveur et
   la date courante.

`DISCORD_CLIENT_SECRET` reste exclusivement côté serveur. Le build Vite ne reçoit que
`VITE_DISCORD_CLIENT_ID`, qui est public.

## Routes

Toutes les routes nécessaires dans l'iframe possèdent un alias `/.proxy/`.

| Route | Rôle |
|---|---|
| `POST /api/token` | échange du code OAuth2 ; |
| `POST /api/activity/session` | création du lien daily signé ; |
| `GET /daily` | page commune aux trois modes ; |
| `POST /daily/start` | verrou de difficulté et départ serveur ; |
| `POST /daily/answer` | validation de l'unique réponse ; |
| `GET /daily/options` | propositions du mode Normal ; |
| `GET /daily/search` | recherche du mode Hardcore ; |
| `GET /daily/context` | messages voisins après la réponse ; |
| `GET /daily/stream` | progression et classement via SSE ; |
| `GET /daily/state` | fallback polling ; |
| `POST /daily/presence` | heartbeat de présence. |

## Interface

La page daily contient trois onglets internes :

- Qui a écrit ça ? ;
- Devine la phrase ;
- Devine le média.

Sur desktop, le classement du mode courant est à gauche, le jeu au centre et le suivi
des participants à droite. La progression live suit les trois modes et applique
l'anti-spoil décrit dans [REALTIME_UPDATES.md](REALTIME_UPDATES.md).

### Rich Presence

Le frontend demande le scope OAuth `rpc.activities.write`, puis appelle
`sdk.commands.setActivity()` après l'authentification. Les visuels utilisent les clés
`1` pour la grande image et `2` pour la petite image, configurées dans **Developer
Portal → Rich Presence → Art Assets**. Un échec de Rich Presence est journalisé mais
ne bloque jamais l'ouverture du daily.

## Média Discord

Les URL de pièces jointes Discord expirent même lorsque le message existe encore.
`GET /daily/media` relit donc le message avec le bot, récupère une URL fraîche puis
transmet le contenu depuis la même origine que l'Activity.

La route :

- refuse toute URL qui n'est pas une pièce jointe officielle Discord ;
- propage les headers `Range` nécessaires aux vidéos ;
- ne transforme pas le serveur en proxy HTTP générique.

En Hardcore média, la limite est de 25 secondes pour une image, 40 secondes pour un
GIF, ou `durée de la vidéo + 25 secondes`. Le total est plafonné à 2 min 30. Pour une
vidéo, le navigateur lit les métadonnées avant le départ et transmet sa durée à
`/daily/start`; le serveur la plafonne puis verrouille la limite avec le premier
départ.

La page propose **Ouvrir le média**, qui lance un lecteur externe ne contenant que
l'image ou la vidéo proxifiée. Il n'affiche ni le message Discord, ni le salon, ni
l'auteur. Dans l'Activity, ce lien passe par
`sdk.commands.openExternalLink()` grâce au bundle `activity-bridge.js` ; hors
Activity, il reste un lien web classique. Le navigateur système peut ainsi prendre
le relais lorsqu'un client Discord lit le son mais ne sait pas décoder la piste
vidéo, sans téléchargement forcé ni transcodage serveur. Le pont tente d'abord une
ouverture synchrone pour conserver l'autorisation liée au clic, puis le SDK ;
Cmd/Ctrl + clic reste disponible si le client bloque malgré tout les deux méthodes.

## Configuration

Variables nécessaires sur le serveur :

```ini
DISCORD_TOKEN=
VITE_DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
WEBAPP_SECRET=
WEBAPP_BASE_URL=https://daily.example.com
WEBAPP_HOST=127.0.0.1
WEBAPP_PORT=8000
WEBAPP_THREADS=64
```

Le Client ID doit appartenir à la même application que le bot et l'Activity déployés.
Les environnements de test et de production doivent chacun utiliser leur propre jeu
d'identifiants cohérent.

## Discord Developer Portal

1. Activer **Activities** pour l'application.
2. Configurer le mapping `/` vers le domaine HTTPS, sans chemin additionnel.
3. Laisser l'URL Override désactivé en production.
4. Vérifier que la commande Entry Point possède le handler Activity.
5. Ajouter les URLs Terms of Service et Privacy Policy.
6. Renommer séparément l'application et le bot si la marque change.

Le script suivant crée ou répare l'Entry Point :

```bash
python scripts/create_entry_point.py
```

## Build et lancement

```bash
cd activity
npm ci
npm run build
cd ..
python bot.py
```

`activity/dist` n'est pas versionné. Le déploiement doit donc construire l'Activity
avant de synchroniser ou redémarrer le service.

## Reverse proxy

- Le port Flask reste lié à `127.0.0.1`.
- Seuls les ports HTTPS publics du reverse proxy sont exposés.
- Le proxy ne doit pas ajouter `X-Frame-Options: DENY`.
- Flask envoie une CSP `frame-ancestors` compatible avec Discord.
- Le buffering doit être désactivé pour `/daily/stream` lorsque le reverse proxy le
  permet.

## Vérifications

Après chaque déploiement :

1. lancer `/daily` depuis Discord ;
2. vérifier l'authentification sans écran blanc ;
3. ouvrir les trois onglets ;
4. démarrer et terminer un mode Normal ;
5. tester le Hardcore et son timeout ;
6. charger une image puis une vidéo ;
7. vérifier le classement et le suivi live avec deux comptes ;
8. confirmer le fallback polling si le proxy bloque le SSE.

Tests locaux :

```bash
python -m unittest discover -s tests -v
cd activity && npm run build
```

## Limites connues

- Le SSE Waitress réserve un thread par viewer ; la configuration vise un groupe
  privé, pas des centaines de connexions.
- Le broker live est mono-process. Un déploiement multi-worker exigerait Redis ou un
  autre bus partagé.
- La validation finale mobile doit être faite dans les clients Discord réellement
  utilisés, car leur iframe peut différer du navigateur desktop.
