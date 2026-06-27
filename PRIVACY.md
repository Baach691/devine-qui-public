# Daily Guessr - Privacy Policy

Last updated: June 27, 2026

This policy explains how the Daily Guessr Discord bot, website and Discord
Activity (together, the "Service") process personal data.

## 1. Data controller and contact

The Daily Guessr project maintainers operate the Service and act as data
controller for its local database.

For a privacy request, open a
[GitHub issue](https://github.com/Baach691/daily-guessr/issues) without
posting a Discord identifier, message or other personal data. Ask for a private
contact method first.

## 2. Data processed

Depending on the enabled features, Daily Guessr may process:

- Discord user, server, channel and message identifiers;
- display names and avatar URLs;
- eligible message text, timestamps and media attachment URLs;
- game answers, correctness, difficulty and response time;
- scores, streaks, rankings and opt-out preferences;
- limited technical logs needed for security and troubleshooting.

Direct messages are not collected.

The Discord OAuth authorization code and access token used by the Activity are
processed only to verify the session and server membership. They are not stored
in the Daily Guessr database.

## 3. Sources and purposes

Data comes from Discord, from channels made available to the bot by server
administrators and from player interactions.

It is used to:

- generate the three daily game modes;
- validate answers and display results;
- maintain scores, streaks and leaderboards;
- enforce one attempt per mode and day;
- verify Activity sessions, roles and server membership;
- correct game errors and diagnose technical incidents.

These operations are necessary to provide the requested Service and support
the legitimate interest of operating and securing a community game.

## 4. Sharing

Game results and leaderboards are visible only to eligible players in the
relevant Discord server after they have played the corresponding challenge.
Authorized administrators may access correction tools.

The Service relies on Discord and on the operator's hosting provider. Those
providers process technical data under their own terms and privacy policies.
Daily Guessr does not sell personal data and does not use advertising or
behavioral analytics.

## 5. Retention

- Eligible source messages remain in the local pool while the Service operates,
  until deletion or `/optout`.
- Challenges, attempts, profiles, scores and streaks remain available to keep
  game history and leaderboards until deletion is requested or the Service is
  discontinued.
- The anti-repetition cache is limited to 500 message identifiers per server
  and game mode.
- Technical logs, when enabled by the operator, should be retained only as long
  as needed for security and troubleshooting.

`/optout` removes source messages from the message pool, but it does not erase
existing attempts, scores or leaderboard entries. A complete deletion request
must be sent to the operator.

## 6. Rights and choices

Depending on applicable law, users may request access, correction, deletion,
restriction, portability or object to processing.

Users can run `/optout` to stop future use of their source messages on a server
and `/optin` to reverse that choice. For complete deletion, contact the operator
using the process described above.

Users in the European Economic Area may also contact their local data
protection authority.

## 7. Security

Daily Guessr uses signed game links, server-side secrets, authenticated
Activity sessions and access checks. Local configuration, databases, aliases
and logs must never be committed to the public repository.

No internet-connected service can guarantee absolute security.

## 8. Cookies and tracking

Daily Guessr does not use advertising cookies or audience analytics. Web game
access uses a signed URL, and the Discord Activity uses Discord OAuth.

## 9. Changes

This policy may be updated when the Service or applicable requirements change.
The date at the top identifies the current version.
