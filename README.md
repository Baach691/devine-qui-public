# Daily Guessr

Daily Guessr est un jeu quotidien pour Discord. Chaque serveur reçoit les mêmes
défis pour tous ses joueurs, avec un essai par mode et par jour, des séries, des
classements et une interface jouable directement dans une Discord Activity.

## Fonctionnalités

- Quatre modes quotidiens :
  - **Qui a écrit ça ?** : retrouver l'auteur d'un message ;
  - **Devine la phrase** : retrouver la phrase écrite par un membre donné ;
  - **Devine le média** : retrouver l'auteur d'une image, d'un GIF ou d'une vidéo ;
  - **Remets dans l'ordre** : replacer cinq messages d'une conversation dans leur
    ordre chronologique, y compris lorsqu'ils contiennent un média.
- Le mode conversation conserve séparément l'ordre proposé et le bon ordre après
  validation. Il attribue de 0 à 5 points selon les positions exactes ; seule une
  partie parfaite compte comme victoire et prolonge la série.
- Mode Hardcore pour les modes auteur et média. Le média accorde 25 secondes pour
  une image, 40 secondes pour un GIF, ou la durée de la vidéo + 25 secondes, avec
  un plafond total de 2 min 30.
- Média agrandissable ou ouvrable dans un lecteur externe sans révéler le message
  Discord ni son auteur.
- Temps de réponse calculé côté serveur.
- Scores, séries, classements complets et résultats du jour.
- Contexte de conversation révélé uniquement après la réponse.
- Tirage automatique des défis à minuit.
- Correction sécurisée des tentatives par les administrateurs autorisés.
- Progression anti-spoil des participants sur les quatre modes, classement à gauche
  et suivi en direct à droite.
- Le direct affiche `×`, `1`, `2`, `3`, `4` ou `✓` pour le mode conversation. Le
  classement indique aussi les joueurs ayant participé aujourd'hui.
- Une fois les quatre modes terminés, chacun peut publier son bilan en emojis,
  sans spoiler, directement dans le salon de l'annonce quotidienne.
- État live initial rendu immédiatement, puis mise à jour par SSE avec polling
  automatique si le proxy Discord bloque le flux.
- Bouton persistant dans l'annonce quotidienne pour lancer directement l'Activity.
- Commande `/optout` pour retirer ses messages du pool local.
- Activity Discord avec OAuth, contrôle d'appartenance au serveur et restriction
  facultative par rôle. La coque reste une application monopage et charge le jeu
  dans une iframe interne pour éviter les navigations bloquées sur mobile.

## Commandes principales

| Commande | Description |
|---|---|
| `/daily` | Lance l'Activity ou fournit le lien web de secours. |
| `/mes-stats` | Affiche les statistiques personnelles des quatre modes. |
| `/classement` | Affiche le classement synthétique. |
| `/classement-complet` | Affiche le classement détaillé. |
| `/daily-resultats` | Affiche les tentatives du jour. |
| `/winner` | Affiche les gagnants du jour. |
| `/loser` | Affiche les perdants du jour. |
| `/backfill` | Alimente le pool depuis l'historique accessible. |
| `/optout` | Retire les messages sources du membre et bloque leur réimportation. |
| `/optin` | Réactive l'utilisation des messages du membre. |

## Architecture

```text
activity/                    Client Vite pour la Discord Activity
cogs/                        Commandes et moteurs des quatre modes
scripts/create_entry_point.py
tests/                       Tests unitaires et d'intégration
webapp/                      Serveur Flask, templates et ressources web
aliases.py                   Chargement facultatif des alias locaux
config.py                    Configuration par variables d'environnement
database.py                  Stockage SQLite
bot.py                       Point d'entrée bot + serveur web
```

Le bot Discord et le serveur web tournent dans le même processus. Cette
architecture permet au site de demander au bot le contexte d'un message sans
exposer de jeton Discord au navigateur.

## Installation locale

```bash
git clone https://github.com/Baach691/daily-guessr.git
cd daily-guessr

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Renseigner au minimum dans `.env` :

```ini
DISCORD_TOKEN=
WEBAPP_SECRET=
WEBAPP_BASE_URL=http://127.0.0.1:8000
```

Puis démarrer :

```bash
.venv/bin/python bot.py
```

Dans le portail développeur Discord :

1. créer une application et son bot ;
2. activer `MESSAGE CONTENT INTENT` ;
3. inviter le bot avec `bot` et `applications.commands` ;
4. autoriser `View Channels`, `Read Message History` et `Send Messages`.

## Configuration

Le fichier [.env.example](./.env.example) documente toutes les variables.

| Variable | Rôle |
|---|---|
| `DISCORD_TOKEN` | Jeton du bot. |
| `WEBAPP_SECRET` | Secret HMAC des liens de jeu. |
| `GUILD_ID` | Serveur facultatif pour synchroniser rapidement les commandes. |
| `DB_PATH` | Chemin de la base SQLite locale. |
| `ALLOWED_CHANNEL_IDS` | Salons utilisables pour les tirages. |
| `BLACKLIST_USER_IDS` | Membres exclus des tirages. |
| `ALLOWED_ROLE_IDS` | Rôles autorisés à jouer. |
| `ADMIN_USER_IDS` | Administrateurs du panneau de correction. |
| `DAILY_PRECOMPUTE_TIME` | Heure locale de préparation des défis. |
| `WEBAPP_BASE_URL` | Origine publique HTTPS du jeu. |
| `WEBAPP_THREADS` | Taille du pool Waitress pour les flux temps réel. |
| `VITE_DISCORD_CLIENT_ID` | ID public de l'application Discord. |
| `DISCORD_CLIENT_SECRET` | Secret OAuth de l'application, côté serveur uniquement. |

Les alias Hardcore peuvent être placés dans `aliases.local.json` à partir de
[aliases.example.json](./aliases.example.json). Ce fichier local est ignoré par Git.

## Discord Activity

Installer et construire le client :

```bash
npm --prefix activity ci
VITE_DISCORD_CLIENT_ID=YOUR_APPLICATION_ID npm --prefix activity run build
```

Configurer ensuite sa propre application Discord :

1. activer les Activities ;
2. déclarer une origine HTTPS contrôlée par l'opérateur ;
3. ajouter le mapping d'URL de l'Activity vers cette origine ;
4. renseigner `VITE_DISCORD_CLIENT_ID` et `DISCORD_CLIENT_SECRET` localement ;
5. créer l'Entry Point :

```bash
.venv/bin/python scripts/create_entry_point.py
```

Les identifiants, domaines et secrets de production ne font volontairement pas
partie de ce dépôt.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile bot.py config.py database.py webapp/server.py
node --check webapp/static/script.js
npm --prefix activity run build
```

## Confidentialité et sécurité

Ne jamais versionner :

- `.env` ou `.webapp_secret` ;
- une base SQLite, ses fichiers WAL ou ses sauvegardes ;
- `aliases.local.json` ;
- des journaux, messages, médias, noms ou identifiants Discord réels ;
- des secrets OAuth, jetons de bot, clés SSH, domaines ou chemins de production.

Le dépôt contient uniquement des valeurs fictives et des exemples génériques.
Consulter également le [journal de reprise](./docs/HANDOFF.md).

## Documents juridiques

- [Conditions d'utilisation](./TERMS.md)
- [Politique de confidentialité](./PRIVACY.md)

Ces fichiers peuvent être renseignés directement dans les champs **Terms of
Service URL** et **Privacy Policy URL** du portail développeur Discord.

## Licence

Daily Guessr est distribué sous [licence MIT](./LICENSE).
