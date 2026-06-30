# Suivi du daily en temps réel

> État : implémenté le 29 juin 2026.
>
> Le site et l'Activity affichent simultanément le classement du mode courant,
> le jeu et la progression des participants sur les quatre modes.

## Expérience

Sur grand écran, la page utilise trois zones stables :

1. le classement complet du mode courant à gauche ;
2. le jeu au centre ;
3. tous les participants du daily à droite.

Le panneau de droite affiche l'avatar, le nom et quatre statuts :

| Statut | Signification |
|---|---|
| `✓` vert | mode réussi ; |
| `×` rouge | mode raté ; |
| sablier jaune | partie en cours ; |
| `✓` gris | mode terminé, résultat encore masqué ; |
| tiret | mode non commencé. |

Pour **Remets dans l'ordre**, le résultat révélé utilise `×` pour 0/5, les chiffres
`1` à `4` pour un ordre partiellement correct et `✓` pour 5/5. Avant que
l'observateur ait lui-même joué ce mode, le statut reste gris et ne révèle aucun
score.

Les joueurs actifs sont placés en premier. Sur un écran plus étroit, les panneaux
latéraux passent sous le jeu afin de conserver une largeur correcte pour les
propositions.

## Anti-spoil

Le payload live est construit pour chaque observateur.

- Tant que l'observateur n'a pas terminé un mode, les résultats terminés des autres
  sont envoyés avec le statut neutre `complete`.
- Dès qu'il termine ce même mode, les statuts deviennent `win` ou `fail`.
- Avant cette réponse, le flux ne contient jamais `guessed_id`, `correct_id`, le
  texte d'une proposition ou le contenu de la bonne réponse.
- Après déblocage du mode, le statut expose uniquement un libellé de guess et le
  temps formaté. L'interface les affiche au survol ou au focus clavier.
- Le classement détaillé et les anciennes données de résultat du mode courant
  restent verrouillés jusqu'à ce que l'observateur ait répondu.

La protection est appliquée côté serveur. Masquer uniquement les éléments en CSS ou
JavaScript ne serait pas suffisant, car les données resteraient visibles dans le
réseau ou le code source.

## Présence

`POST /daily/presence` reçoit un heartbeat signé toutes les 15 secondes. Son alias
historique `/.proxy/daily/presence` reste accepté.

- Une présence expire après 45 secondes sans heartbeat.
- Une ouverture de page marque le mode consulté.
- `POST /daily/start` marque le mode comme en cours.
- `POST /daily/answer` marque le mode comme terminé.
- Les tentatives terminées viennent de SQLite et restent donc visibles après un
  redémarrage ; seule la présence instantanée est conservée en mémoire.

Le serveur envoie aussi un snapshot SSE toutes les 15 secondes. Cela retire les
présences expirées même lorsqu'aucune nouvelle réponse n'a été enregistrée.

## Transport

Le transport principal reste Server-Sent Events :

```text
GET /daily/stream?t=<token>
```

La clé du broker est désormais `(guild_id, date)`. Un démarrage ou une réponse dans
n'importe lequel des quatre modes réveille donc tous les viewers du daily.

Chaque signal provoque un recalcul personnalisé :

```json
{
  "unlocked": false,
  "results": [],
  "leaderboard": [],
  "participant_count": 3,
  "progress": [
    {
      "user_id": "123",
      "name": "Joueur",
      "avatar_url": "https://...",
      "active": true,
      "playing": true,
      "activity": "Devine la phrase en cours",
      "statuses": {
        "author": "complete",
        "phrase": "playing",
        "media": "waiting",
        "sequence": "waiting"
      },
      "details": {},
      "is_me": false
    }
  ]
}
```

Quand le viewer a terminé le mode courant, `unlocked` passe à `true` et les champs
`results` et `leaderboard` sont également remplis. Pour chaque mode déjà terminé,
`details[mode]` peut alors contenir :

```json
{
  "guess": "Réponse choisie",
  "time": "1.200s"
}
```

## Fallback

Si le proxy Discord bufferise ou bloque le SSE, le client bascule automatiquement
sur :

```text
GET /daily/state?t=<token>
```

Le polling a lieu toutes les trois secondes, s'arrête lorsque la page est masquée et
reprend lorsqu'elle redevient visible.

## Contraintes d'exploitation

- Le broker et la présence sont en mémoire : le déploiement doit rester mono-process.
- Waitress réserve un thread par connexion SSE. `WEBAPP_THREADS=64` convient à un
  groupe d'amis, pas à un service public de grande taille.
- Le bot et Flask restent dans le même process, car `/daily/context` utilise la loop
  Discord du bot.
- Un passage futur en multi-worker nécessiterait un stockage partagé, par exemple
  Redis pour le pub/sub et les présences.

## Événements publiés

Un signal global `(guild_id, date)` est publié après :

- une nouvelle présence visible ou un changement de mode ;
- le clic sur **Jouer** ;
- l'enregistrement d'une réponse ;
- une correction administrateur.

Les simples heartbeats renouvellent la date d'expiration sans réveiller tout le
monde.

La page reçoit aussi un premier état personnalisé dans `window.DAILY` au rendu.
La colonne En direct est donc remplie immédiatement ; le SSE prend ensuite le relais
sans modifier les règles anti-spoil.

## Partage du résultat

Une fois les quatre modes terminés, seule la ligne du viewer reçoit
`can_share: true`. Le bouton appelle `POST /daily/share` avec le token signé.

- Le message ne contient que les résultats emoji, jamais les réponses choisies.
- Le salon est celui mémorisé lors du ping quotidien ; les anciennes annonces
  utilisent le même ordre de repli que le scheduler.
- La contrainte SQLite `(guild_id, date, user_id)` rend le partage idempotent.
- Une réservation est annulée si Discord refuse l'envoi, afin de permettre un nouvel
  essai.
- Après succès, le flux live remplace le bouton par l'état `Partagé`.

## Tests

`tests/test_realtime_updates.py` vérifie notamment :

- l'accès au flux avant d'avoir répondu, avec résultats et classement verrouillés ;
- le masquage d'une victoire ou d'une défaite avant la réponse du viewer ;
- la révélation après avoir terminé le même mode ;
- le statut « en cours » après `/daily/start` ;
- l'expiration d'une présence devenue silencieuse ;
- l'indépendance de l'anti-spoil pour chacun des quatre modes ;
- l'absence des identifiants de réponse dans le payload protégé ;
- la publication commune aux quatre modes ;
- le désabonnement du flux lors de la fermeture.
- l'état initial anti-spoil inclus dans la page avant la connexion SSE.

Commande :

```bash
python -m unittest tests.test_realtime_updates -v
```

## Validation après déploiement

1. Ouvrir l'Activity avec deux comptes sur le même serveur.
2. Commencer des modes différents.
3. Vérifier que les deux sabliers apparaissent sans rechargement.
4. Terminer un mode avec le premier compte.
5. Vérifier que le second voit un résultat gris tant qu'il n'a pas joué ce mode.
6. Terminer le même mode avec le second compte et vérifier que les résultats réels
   deviennent visibles.
7. Fermer un compte et vérifier sa disparition des joueurs actifs après environ une
   minute.
