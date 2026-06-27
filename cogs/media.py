"""Mode de jeu annexe "devine le média".

Identique au mode « Qui a écrit ça ? » (on devine l'auteur), mais le défi affiche
un média (image/gif/vidéo) au lieu d'un texte. Tables media_* isolées : toutes
les stats (victoires, défaites, streaks, temps…) sont enregistrées comme si le
mode participait au classement, pour une intégration future. Pour l'instant il
n'apparaît PAS dans /classement.
"""

import logging
import random
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

import discord

import config
import database
import filters
from cogs.daily import _pickable_channels, _user_avatar_url, global_name

log = logging.getLogger(__name__)

MODE = database.MODE_MEDIA


async def _pick_media_daily_live(
    guild_id: int, channels: List[discord.TextChannel]
) -> Optional[Tuple[discord.Message, str, List[Tuple[int, str]]]]:
    """Tire un message contenant un média éligible + 4 auteurs proposés.

    Renvoie (message, media_url, options) ou None.
    """
    if not channels:
        return None
    try:
        oldest = date.fromisoformat(config.OLDEST_MESSAGE_DATE)
    except Exception:
        oldest = date(2023, 10, 1)
    today = date.today()
    span_days = (today - oldest).days
    if span_days <= 0:
        return None

    blacklist = set(getattr(config, "BLACKLIST_USER_IDS", []) or [])
    recent_ids = database.get_recent_picks_set(
        guild_id, limit=config.RECENT_PICKS_EXCLUDE, mode=MODE
    )

    for attempt_idx in range(3):
        channel = random.choice(channels)
        offset = random.randint(0, span_days)
        target = oldest + timedelta(days=offset)

        for window in (15, 45, 120):
            low_d = max(oldest, target - timedelta(days=window))
            high_d = min(today + timedelta(days=1), target + timedelta(days=window + 1))
            after_dt = datetime.combine(low_d, datetime.min.time(), tzinfo=timezone.utc)
            before_dt = datetime.combine(high_d, datetime.min.time(), tzinfo=timezone.utc)

            scanned = 0
            media_msgs: List[Tuple[discord.Message, str]] = []
            authors_seen: dict = {}
            try:
                async for msg in channel.history(
                    after=after_dt, before=before_dt, limit=200, oldest_first=False
                ):
                    scanned += 1
                    if msg.author.id in blacklist:
                        continue
                    if msg.id in recent_ids:
                        continue
                    if database.is_opted_out(msg.author.id, guild_id):
                        continue
                    url = filters.media_attachment_url(msg)
                    if url is None:
                        continue
                    media_msgs.append((msg, url))
                    authors_seen[msg.author.id] = msg.author
            except discord.DiscordException as e:
                log.warning("Média: erreur fetch (#%s, %s): %s", channel.name, target, e)
                continue

            log.info(
                "Média pick #%d ±%dj autour de %s dans #%s : scannés=%d, médias=%d, auteurs=%d",
                attempt_idx + 1, window, target, channel.name,
                scanned, len(media_msgs), len(authors_seen),
            )

            if not media_msgs:
                continue

            daily_msg, media_url = random.choice(media_msgs)
            daily_author_id = daily_msg.author.id
            for aid, user in authors_seen.items():
                database.upsert_user(guild_id, aid, global_name(user), _user_avatar_url(user))

            # Distracteurs : autres auteurs vus, complétés depuis la BDD.
            distractor_pool: dict = {
                aid: global_name(u) for aid, u in authors_seen.items() if aid != daily_author_id
            }
            if len(distractor_pool) < 3:
                for aid, name in database.get_distinct_authors(
                    guild_id, exclude_id=daily_author_id, limit=10
                ):
                    if len(distractor_pool) >= 3:
                        break
                    if aid != daily_author_id and aid not in distractor_pool:
                        distractor_pool[aid] = name
            if not distractor_pool:
                continue

            ids = random.sample(list(distractor_pool.keys()), k=min(3, len(distractor_pool)))
            options = [(aid, distractor_pool[aid]) for aid in ids]
            options.append((daily_author_id, global_name(daily_msg.author)))
            random.shuffle(options)
            return daily_msg, media_url, options

    return None


async def ensure_media_daily_for_guild(
    guild: discord.Guild, date_str: str, *, label: str = "daily"
) -> Tuple[bool, Optional[str]]:
    """Crée le défi média du jour si besoin. Renvoie (ok, erreur_affichable)."""
    if database.get_daily(guild.id, date_str, mode=MODE) is not None:
        return True, None
    channels = _pickable_channels(guild)
    if not channels:
        return False, "Aucun salon accessible pour préparer le mode média."
    try:
        picked = await _pick_media_daily_live(guild.id, channels)
    except Exception:
        log.exception("Média (%s) : exception pour %s.", label, guild.name)
        return False, "Une erreur est survenue pendant le tirage du mode média."
    if picked is None:
        log.warning("Média (%s) : aucun média éligible pour %s.", label, guild.name)
        return False, "Pas assez de médias (images/gifs/vidéos) postés pour ce mode."

    msg, media_url, options = picked
    created = database.create_daily_if_absent(
        guild.id, date_str, msg.id, msg.channel.id,
        msg.author.id, global_name(msg.author), media_url, options, mode=MODE,
    )
    if created:
        database.record_pick(guild.id, msg.id, mode=MODE)
        log.info("Média (%s) ✅ pour %s : msg #%s de %s.",
                 label, guild.name, msg.id, global_name(msg.author))
    return database.get_daily(guild.id, date_str, mode=MODE) is not None, None


# Plus de cog/scheduling ici : le mode média est pré-calculé par le cog Daily
# (scheduler unique) et joué via /daily + le site. Ce module n'expose que la
# logique de tirage (ensure_media_daily_for_guild / _pick_media_daily_live).
