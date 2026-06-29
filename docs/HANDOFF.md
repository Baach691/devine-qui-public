# Daily Guessr - public repository status

Last updated: June 29, 2026.

## Public scope

This repository contains the reusable source code for:

- the Discord bot and its three daily game modes;
- the Flask web game and administration tools;
- the Discord Activity client and OAuth session bridge;
- real-time result updates with an automatic polling fallback;
- spoiler-safe progress tracking across all three modes;
- automated tests and generic configuration examples.

## Privacy boundary

Never commit:

- `.env`, databases, logs, local aliases or private media;
- production domains, IP addresses, server paths or deployment workflows;
- real Discord application, guild, channel, role or user identifiers;
- real names, usernames, avatars, messages or friend-group details;
- OAuth secrets, bot tokens, SSH keys or signed game URLs.

All tests and examples must use clearly fictional identities and identifiers.

## Migration status

- [x] Brand selected: `Daily Guessr`.
- [x] Author mode label changed to `Qui a écrit ça ?`.
- [x] Discord Activity client and backend bridge added.
- [x] `/daily` can launch the configured Discord Activity.
- [x] Admin corrections and real-time leaderboard updates included.
- [x] Live presence, three-mode progress and server-side anti-spoiler rules.
- [x] Three-panel responsive layout and corrected leaderboard alignment.
- [x] Hardcore media timer: image 25 s, GIF 40 s, video duration + 25 s, capped
  at 2 min 30.
- [x] Rich Presence artwork keys `1` and `2`.
- [x] Spoiler-free external media viewer through the Activity SDK, without a
  Discord message link, forced download or server-side transcoding.
- [x] Initial live state embedded in the page before the SSE connection.
- [x] Persistent Play button in the daily announcement.
- [x] `TERMS.md` and `PRIVACY.md` added.
- [x] VPS-specific deployment files removed.
- [x] Final working-tree secret and identity scan.
- [x] 32 Python tests, syntax checks and Activity build verification.
- [x] Publish the sanitized root commit.

## External setup still required

Each operator must create their own Discord application, OAuth credentials,
HTTPS host and URL mapping. No production identifier or host is provided here.
