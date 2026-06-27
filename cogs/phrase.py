"""Mode de jeu "devine la phrase".

Symétrique du daily "qui a écrit ça ?", mais on inverse la question : on tire un
utilisateur cible, et 4 phrases (1 de lui + 3 d'autres) ; le joueur doit deviner
laquelle l'utilisateur cible a écrite.

Tout est isolé dans des tables `phrase_*` (mode="phrase") : streaks, scores,
classement, annonces, anti-répétition indépendants du mode historique.
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

MODE = database.MODE_PHRASE


async def _pick_phrase_daily_live(
    guild_id: int, channels: List[discord.TextChannel]
) -> Optional[Tuple[int, str, discord.Message, List[Tuple[int, str]]]]:
    """Tire un défi "phrase" : un utilisateur cible + 4 phrases.

    Renvoie (target_author_id, target_author_name, correct_msg, options) où
    options = [(message_id, texte), ...] (la bonne + jusqu'à 3 distractrices),
    ou None si rien d'exploitable (serveur trop petit, etc.).
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

        for window in (7, 30, 90):
            low_d = max(oldest, target - timedelta(days=window))
            high_d = min(today + timedelta(days=1), target + timedelta(days=window + 1))
            after_dt = datetime.combine(low_d, datetime.min.time(), tzinfo=timezone.utc)
            before_dt = datetime.combine(high_d, datetime.min.time(), tzinfo=timezone.utc)

            scanned = 0
            by_author: dict = {}  # author_id -> list[discord.Message]
            try:
                async for msg in channel.history(
                    after=after_dt, before=before_dt, limit=200, oldest_first=False
                ):
                    scanned += 1
                    if msg.author.bot:
                        continue
                    if msg.author.id in blacklist:
                        continue
                    if msg.id in recent_ids:
                        continue
                    if database.is_opted_out(msg.author.id, guild_id):
                        continue
                    if not filters.is_eligible(msg, config.MIN_CHARS, config.MIN_WORDS):
                        continue
                    by_author.setdefault(msg.author.id, []).append(msg)
            except discord.DiscordException as e:
                log.warning("Phrase: erreur fetch (#%s, %s): %s", channel.name, target, e)
                continue

            log.info(
                "Phrase pick #%d ±%dj autour de %s dans #%s : scannés=%d, auteurs=%d",
                attempt_idx + 1, window, target, channel.name, scanned, len(by_author),
            )

            # Il faut le cible + au moins un autre auteur pour avoir un distracteur.
            if len(by_author) < 2:
                continue

            target_id = random.choice(list(by_author.keys()))
            correct_msg = random.choice(by_author[target_id])
            for aid, messages in by_author.items():
                author = messages[0].author
                database.upsert_user(guild_id, aid, global_name(author), _user_avatar_url(author))

            # Distracteurs : phrases d'autres auteurs (jusqu'à 3, auteurs distincts).
            other_authors = [aid for aid in by_author if aid != target_id]
            random.shuffle(other_authors)
            distractors: List[discord.Message] = []
            for aid in other_authors:
                if len(distractors) >= 3:
                    break
                distractors.append(random.choice(by_author[aid]))

            if not distractors:
                continue

            # On stocke aussi l'auteur de chaque phrase (pour afficher sa pp au reveal).
            options = [(m.id, _truncate(m.content), m.author.id) for m in distractors]
            options.append((correct_msg.id, _truncate(correct_msg.content), correct_msg.author.id))
            random.shuffle(options)

            return target_id, global_name(correct_msg.author), correct_msg, options

    return None


def _truncate(text: str, limit: int = 280) -> str:
    """Tronque une phrase trop longue pour rester lisible dans un bouton/carte."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def ensure_phrase_daily_for_guild(
    guild: discord.Guild, date_str: str, *, label: str = "daily"
) -> Tuple[bool, Optional[str]]:
    """Crée le défi phrase du jour si besoin.

    Renvoie (ok, erreur_affichable). La commande `/daily` s'en sert pour préparer
    les deux onglets du site sans exposer une commande `/phrase` séparée.
    """
    if database.get_phrase_daily(guild.id, date_str) is not None:
        return True, None

    channels = _pickable_channels(guild)
    if not channels:
        log.warning("Phrase (%s) : aucun salon pour %s.", label, guild.name)
        return False, "Aucun salon accessible pour préparer le mode phrase."

    try:
        picked = await _pick_phrase_daily_live(guild.id, channels)
    except Exception:
        log.exception("Phrase (%s) : exception pour %s.", label, guild.name)
        return False, "Une erreur est survenue pendant le tirage du mode phrase."

    if picked is None:
        log.warning(
            "Phrase (%s) : rien d'éligible pour %s (le prochain /daily retentera).",
            label, guild.name,
        )
        return (
            False,
            "Pas assez de messages éligibles d'auteurs différents pour le mode phrase.",
        )

    target_id, target_name, correct_msg, options = picked
    created = database.create_phrase_daily_if_absent(
        guild.id, date_str, target_id, target_name,
        correct_msg.id, correct_msg.channel.id,
        _truncate(correct_msg.content), options,
    )
    if created:
        database.record_pick(guild.id, correct_msg.id, mode=MODE)
        log.info(
            "Phrase (%s) ✅ pour %s : cible %s, phrase #%s.",
            label, guild.name, target_name, correct_msg.id,
        )
    return database.get_phrase_daily(guild.id, date_str) is not None, None


# Plus de cog/scheduling ici : le mode phrase est pré-calculé par le cog Daily
# (scheduler unique) et joué via /daily + le site. Ce module n'expose que la
# logique de tirage (ensure_phrase_daily_for_guild / _pick_phrase_daily_live).
