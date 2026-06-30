"""Serveur Flask qui sert l'interface graphique du daily.

- GET  /daily?t=<token>          → page de jeu (ou page de résultat si déjà joué)
- POST /daily/answer (JSON)      → enregistre la réponse et renvoie les stats
- GET  /                         → page d'accueil minimale (rediriger vers Discord)

Les liens sont signés HMAC : impossible de tricher sur l'identité.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

import discord
from flask import (
    Flask,
    Response,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
)

import config
import database
import filters
import tokens
from cogs.daily import (
    _build_daily_link,
    today_str, streak_emoji, loss_streak_emoji, format_duration_ms, format_date_fr,
)

log = logging.getLogger(__name__)

RealtimeKey = Tuple[int, str]
PresenceKey = Tuple[int, str, int]
_realtime_subscribers: Dict[RealtimeKey, Set[queue.Queue]] = {}
_realtime_lock = threading.Lock()
_live_presence: Dict[PresenceKey, dict] = {}
_presence_lock = threading.Lock()
_PRESENCE_TTL_SECONDS = 45
_MEDIA_HARDCORE_BASE_SECONDS = 25
_MEDIA_HARDCORE_GIF_BONUS_SECONDS = 15
_MEDIA_HARDCORE_MAX_SECONDS = 2 * 60 + 30

DAILY_MODE_SPECS = (
    (database.MODE_AUTHOR, "🌞", "Qui a écrit ça ?"),
    (database.MODE_PHRASE, "✍️", "Devine la phrase"),
    (database.MODE_MEDIA, "🖼️", "Devine le média"),
    (database.MODE_SEQUENCE, "🔀", "Remets dans l'ordre"),
)


def _subscribe_realtime(key: RealtimeKey) -> queue.Queue:
    """Abonne un flux à une partie, avec une file bornée qui fusionne les signaux."""
    subscriber = queue.Queue(maxsize=1)
    with _realtime_lock:
        _realtime_subscribers.setdefault(key, set()).add(subscriber)
    return subscriber


def _unsubscribe_realtime(key: RealtimeKey, subscriber: queue.Queue) -> None:
    """Retire un flux déconnecté et nettoie les clés devenues vides."""
    with _realtime_lock:
        subscribers = _realtime_subscribers.get(key)
        if subscribers is None:
            return
        subscribers.discard(subscriber)
        if not subscribers:
            _realtime_subscribers.pop(key, None)


def _publish_realtime(key: RealtimeKey) -> None:
    """Signale un changement sans accumuler plusieurs notifications identiques."""
    with _realtime_lock:
        subscribers = tuple(_realtime_subscribers.get(key, ()))
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(None)
        except queue.Full:
            pass


def _touch_presence(
    guild_id: int,
    date_str: str,
    user_id: int,
    mode: str,
    playing: Optional[bool] = None,
) -> bool:
    """Marque un joueur présent et renvoie True si son état visible a changé."""
    key = (guild_id, date_str, user_id)
    now = time.monotonic()
    with _presence_lock:
        previous = _live_presence.get(key)
        effective_playing = (
            bool(playing)
            if playing is not None
            else bool(previous and previous.get("playing"))
        )
        changed = (
            previous is None
            or previous["mode"] != mode
            or previous.get("playing", False) != effective_playing
        )
        _live_presence[key] = {
            "mode": mode,
            "playing": effective_playing,
            "seen_at": now,
        }
    return changed


def _active_presence(guild_id: int, date_str: str) -> Dict[int, dict]:
    """Renvoie les présences récentes en supprimant les heartbeats expirés."""
    now = time.monotonic()
    active = {}
    with _presence_lock:
        expired = [
            key
            for key, presence in _live_presence.items()
            if now - presence["seen_at"] > _PRESENCE_TTL_SECONDS
        ]
        for key in expired:
            _live_presence.pop(key, None)
        for (
            presence_guild,
            presence_date,
            user_id,
        ), presence in _live_presence.items():
            if presence_guild == guild_id and presence_date == date_str:
                active[user_id] = dict(presence)
    return active


def default_avatar(user_id: int) -> str:
    """Avatar Discord par défaut (basé sur l'ID utilisateur)."""
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


def avatar_for_user(guild_id: int, user_id: int) -> str:
    """Avatar connu en base, fallback sur l'avatar Discord par défaut."""
    user = database.get_user(guild_id, user_id)
    return (user or {}).get("avatar_url") or default_avatar(user_id)


def _enrich_results(results):
    """Garantit qu'un avatar (URL) est présent sur chaque ligne (fallback default).

    Ajoute aussi une version formatée du temps de réponse (`time_taken_str`).
    """
    for r in results:
        if not r.get("avatar_url"):
            r["avatar_url"] = default_avatar(r["user_id"])
        r["time_taken_str"] = format_duration_ms(r.get("time_taken_ms"))
    return results


# --- Contexte de conversation : ±5 messages autour du daily ---------------

def _msg_to_dict(msg: discord.Message) -> dict:
    """Sérialise un message Discord pour le rendu HTML du contexte."""
    author = msg.author
    name = getattr(author, "global_name", None) or author.name
    avatar = author.display_avatar.url if author.display_avatar else default_avatar(author.id)
    content = (msg.content or "").strip()
    has_attachment = bool(msg.attachments) or bool(msg.embeds) or bool(msg.stickers)
    if not content and has_attachment:
        content = "📎 (pièce jointe / embed)"
    elif has_attachment:
        content = f"{content}  📎"
    return {
        "author_name": name,
        "avatar_url": avatar,
        "content": content,
        "created_at": msg.created_at.isoformat(),
        "message_id": msg.id,
    }


async def _fetch_context_async(
    bot, channel_id: int, message_id: int, n: int = 5
) -> Tuple[List[dict], List[dict]]:
    """Récupère ±N messages autour du target depuis Discord (live)."""
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.DiscordException:
            return [], []

    msg_ref = discord.Object(id=message_id)

    before_msgs: List[dict] = []
    try:
        async for msg in channel.history(before=msg_ref, limit=n):
            before_msgs.append(_msg_to_dict(msg))
    except discord.DiscordException:
        pass
    before_msgs.reverse()  # chronologique

    after_msgs: List[dict] = []
    try:
        async for msg in channel.history(after=msg_ref, limit=n, oldest_first=True):
            after_msgs.append(_msg_to_dict(msg))
    except discord.DiscordException:
        pass

    return before_msgs, after_msgs


def fetch_message_context(
    bot, channel_id: int, message_id: int, n: int = 5
) -> Tuple[List[dict], List[dict]]:
    """Version synchrone : exécute le fetch async sur l'event loop du bot.

    Renvoie ([], []) si le bot n'est pas prêt ou si l'appel Discord échoue.
    """
    if bot is None or not bot.is_ready():
        return [], []
    try:
        coro = _fetch_context_async(bot, channel_id, message_id, n)
        future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
        return future.result(timeout=8.0)
    except Exception:
        log.exception("Échec du fetch de contexte (channel=%s, msg=%s)", channel_id, message_id)
        return [], []


async def _fetch_current_media_url_async(
    bot, channel_id: int, message_id: int
) -> Optional[str]:
    """Relit le message Discord pour obtenir une URL de pièce jointe fraîche."""
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    message = await channel.fetch_message(message_id)
    return filters.media_attachment_url(message)


def fetch_current_media_url(
    bot, channel_id: int, message_id: int
) -> Optional[str]:
    """Version synchrone du rafraîchissement d'URL média Discord."""
    if bot is None or not bot.is_ready():
        return None
    try:
        future = asyncio.run_coroutine_threadsafe(
            _fetch_current_media_url_async(bot, channel_id, message_id),
            bot.loop,
        )
        return future.result(timeout=8.0)
    except Exception:
        log.exception(
            "Échec du rafraîchissement média (channel=%s, msg=%s)",
            channel_id,
            message_id,
        )
        return None


def _daily_result_channel_id(guild_id: int, date_str: str) -> Optional[int]:
    """Retrouve le salon du ping quotidien, avec repli pour les anciennes annonces."""
    candidates = []
    announced_channel = database.get_daily_announce_channel(guild_id, date_str)
    if announced_channel:
        candidates.append(announced_channel)
    explicit = getattr(config, "ANNOUNCE_CHANNEL_ID", 0) or 0
    if explicit:
        candidates.append(explicit)
    candidates.extend(getattr(config, "ALLOWED_CHANNEL_IDS", []) or [])
    daily = database.get_daily(guild_id, date_str)
    if daily is not None:
        candidates.append(daily["channel_id"])
    return int(candidates[0]) if candidates else None


def _format_daily_result_share(
    date_str: str,
    user_name: str,
    attempts: Dict[str, dict],
) -> str:
    """Résumé sans spoil, inspiré des partages Wordle."""
    sequence_score = max(
        0,
        min(5, int(attempts[database.MODE_SEQUENCE].get("guessed_id") or 0)),
    )
    sequence_icons = {
        0: "❌",
        1: "1️⃣",
        2: "2️⃣",
        3: "3️⃣",
        4: "4️⃣",
        5: "✅",
    }
    safe_name = "".join(
        f"\\{character}" if character in "\\*_~`>|" else character
        for character in user_name
    )
    lines = [
        f"🎮 **Daily Guessr — {format_date_fr(date_str)}**",
        f"**{safe_name}**",
        " ".join((
            f"🌞 {'✅' if attempts[database.MODE_AUTHOR]['correct'] else '❌'}",
            f"✍️ {'✅' if attempts[database.MODE_PHRASE]['correct'] else '❌'}",
            f"🖼️ {'✅' if attempts[database.MODE_MEDIA]['correct'] else '❌'}",
            f"🔀 {sequence_icons[sequence_score]}/5",
        )),
    ]
    wins = sum(1 for attempt in attempts.values() if attempt["correct"])
    lines.append(
        f"{'🏆' if wins == len(DAILY_MODE_SPECS) else '🏁'} "
        f"**{wins}/{len(DAILY_MODE_SPECS)} modes réussis**"
    )
    return "\n".join(lines)


async def _send_daily_result_async(bot, channel_id: int, content: str) -> int:
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    message = await channel.send(
        content,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return int(message.id)


def send_daily_result(bot, channel_id: int, content: str) -> Optional[int]:
    """Envoie le partage depuis le thread Flask via l'event loop Discord."""
    if bot is None or not bot.is_ready():
        return None
    try:
        future = asyncio.run_coroutine_threadsafe(
            _send_daily_result_async(bot, channel_id, content),
            bot.loop,
        )
        return future.result(timeout=8.0)
    except Exception:
        log.exception("Échec du partage du résultat dans le salon %s", channel_id)
        return None


def _is_discord_attachment_url(url: str) -> bool:
    """Empêche la route média de devenir un proxy HTTP arbitraire."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"cdn.discordapp.com", "media.discordapp.net"}
        and parsed.path.startswith("/attachments/")
    )


# --- Sidebar : classement live --------------------------------------------

def _leaderboard_view(
    guild_id: int,
    mode: str,
    me_user_id: int,
    top: Optional[int] = None,
    date_str: Optional[str] = None,
) -> list:
    """Classement du mode courant prêt à afficher dans la sidebar."""
    rows = database.get_leaderboard(guild_id, limit=top, mode=mode)
    played_today = {
        int(result["user_id"])
        for result in database.get_daily_results(guild_id, date_str, mode=mode)
    } if date_str else set()
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            "rank": i,
            "name": r["name"],
            "avatar_url": avatar_for_user(guild_id, r["user_id"]),
            "correct": r["correct"],
            "total": r["total"],
            "points": r["points"],
            "current_streak": r["current_streak"],
            "current_loss_streak": r.get("current_loss_streak", 0) or 0,
            "is_me": r["user_id"] == me_user_id,
            "played_today": r["user_id"] in played_today,
        })
    return out


# --- Multi-mode -----------------------------------------------------------

def _payload_mode(payload: dict) -> str:
    """Mode lu depuis le token (clé 'm'). Défaut = author (rétro-compat)."""
    mode = payload.get("m", database.MODE_AUTHOR)
    return mode if mode in database.VALID_MODES else database.MODE_AUTHOR


def _load_challenge(guild_id: int, today: str, mode: str) -> Optional[dict]:
    """Charge le défi du jour, normalisé pour le rendu commun aux quatre modes.

    Champs renvoyés :
      correct_id      : id de la bonne option (auteur ou message selon le mode)
      correct_name    : nom à révéler
      options         : [(id, label), ...]
      message_content : message mystère (mode author) ou None (mode phrase)
      subject_id      : utilisateur cible (mode phrase) ou None
      subject_name    : utilisateur cible (mode phrase) ou None
      subject_avatar_url : avatar utilisateur cible (mode phrase) ou ""
      channel_id / message_id : pour le contexte au reveal
      content_reveal  : texte de la bonne réponse (message ou phrase)
    """
    if mode == database.MODE_SEQUENCE:
        daily = database.get_sequence_daily(guild_id, today)
        if daily is None:
            return None
        return {
            "correct_id": 5,
            "correct_name": "les 5 messages dans le bon ordre",
            "options": [
                (1, "1/5 bien placé"),
                (2, "2/5 bien placés"),
                (3, "3/5 bien placés"),
                (4, "4/5 bien placés"),
                (5, "Ordre correct"),
            ],
            "message_content": None,
            "is_media": False,
            "media_url": "",
            "media_is_video": False,
            "media_is_gif": False,
            "is_sequence": True,
            "sequence_messages": daily["messages"],
            "subject_id": None,
            "subject_name": None,
            "subject_avatar_url": "",
            "channel_id": daily["channel_id"],
            "message_id": daily["first_message_id"],
            "content_reveal": "Conversation remise dans l'ordre",
        }
    if mode == database.MODE_PHRASE:
        d = database.get_phrase_daily(guild_id, today)
        if d is None:
            return None
        return {
            "correct_id": d["correct_id"],
            "correct_name": d["author_name"],
            "options": d["options"],
            "message_content": None,
            "is_media": False,
            "media_url": "",
            "media_is_video": False,
            "media_is_gif": False,
            "is_sequence": False,
            "subject_id": d["author_id"],
            "subject_name": d["author_name"],
            "subject_avatar_url": avatar_for_user(guild_id, d["author_id"]),
            "channel_id": d["channel_id"],
            "message_id": d["message_id"],
            "content_reveal": d["content"],
        }
    # author + media partagent get_daily (table daily / media_daily).
    d = database.get_daily(guild_id, today, mode=mode)
    if d is None:
        return None
    is_media = (mode == database.MODE_MEDIA)
    return {
        "correct_id": d["author_id"],
        "correct_name": d["author_name"],
        "options": d["options"],
        "message_content": None if is_media else d["content"],
        "is_media": is_media,
        "media_url": d["content"] if is_media else "",
        "media_is_video": _is_video_url(d["content"]) if is_media else False,
        "media_is_gif": _is_gif_url(d["content"]) if is_media else False,
        "is_sequence": False,
        "subject_id": None,
        "subject_name": None,
        "subject_avatar_url": "",
        "channel_id": d["channel_id"],
        "message_id": d["message_id"],
        "content_reveal": ("🖼️ média" if is_media else d["content"]),
    }


def _is_video_url(url: str) -> bool:
    u = (url or "").split("?")[0].lower()
    return u.endswith((".mp4", ".mov", ".webm", ".mkv", ".m4v"))


def _is_gif_url(url: str) -> bool:
    return (url or "").split("?")[0].lower().endswith(".gif")


def _hardcore_base_seconds(mode: str) -> int:
    if mode == database.MODE_MEDIA:
        return _MEDIA_HARDCORE_BASE_SECONDS
    return config.HARDCORE_TIME_LIMIT


def _hardcore_limit_seconds(
    guild_id: int,
    date_str: str,
    user_id: int,
    mode: str,
) -> float:
    limit = (
        _hardcore_base_seconds(mode)
        + database.get_daily_time_bonus_seconds(
            guild_id, date_str, user_id, mode
        )
    )
    if mode == database.MODE_MEDIA:
        return min(limit, _MEDIA_HARDCORE_MAX_SECONDS)
    return limit


def _options_view(guild_id: int, options: list, mode: str) -> list:
    """Options prêtes pour le template.

    - author/média : avatar de l'auteur proposé (visible pendant le jeu).
    - phrase : avatar de l'auteur de CHAQUE phrase, mais 'reveal_only' (affiché
      seulement après réponse, sinon ce serait un spoil). Tolère les anciennes
      options sans auteur (2-uplets).
    """
    out = []
    for opt in options:
        opt_id, label = opt[0], opt[1]
        author_id = opt[2] if len(opt) > 2 else None
        avatar = ""
        reveal_only = False
        if mode in (database.MODE_AUTHOR, database.MODE_MEDIA):
            avatar = avatar_for_user(guild_id, opt_id)  # l'option EST l'auteur
        elif mode == database.MODE_PHRASE and author_id is not None:
            avatar = avatar_for_user(guild_id, author_id)
            reveal_only = True
        out.append({
            "id": opt_id,
            "label": label,
            "avatar_url": avatar,
            "reveal_only": reveal_only,
        })
    return out


def _sequence_messages_view(
    guild_id: int,
    date_str: str,
    user_id: int,
    token: str,
    challenge: dict,
    order_ids: Optional[list] = None,
    *,
    shuffled: bool = False,
) -> list:
    """Décore les messages dans l'ordre demandé, avec URLs média signées."""
    canonical = [dict(message) for message in challenge["sequence_messages"]]
    by_id = {str(message["id"]): message for message in canonical}
    if order_ids is not None:
        ordered = [
            by_id[str(message_id)]
            for message_id in order_ids
            if str(message_id) in by_id
        ]
    elif shuffled:
        ordered = list(canonical)
        seed_material = (
            f"{config.WEBAPP_SECRET}|{guild_id}|{date_str}|{user_id}|sequence"
        )
        seed = int.from_bytes(
            hashlib.sha256(seed_material.encode("utf-8")).digest()[:8],
            "big",
        )
        random.Random(seed).shuffle(ordered)
        if [str(message["id"]) for message in ordered] == list(by_id):
            ordered[-2], ordered[-1] = ordered[-1], ordered[-2]
    else:
        ordered = canonical

    out = []
    for position, message in enumerate(ordered, 1):
        author_id = int(message["author_id"])
        item = {
            "id": str(message["id"]),
            "position": position,
            "author_name": message["author_name"],
            "author_avatar_url": avatar_for_user(guild_id, author_id),
            "content": message.get("content", ""),
            "has_media": bool(message.get("has_media")),
            "media_is_video": bool(message.get("media_is_video")),
            "media_url": "",
            "position_correct": (
                str(message["id"]) == str(canonical[position - 1]["id"])
            ),
        }
        if item["has_media"]:
            item["media_url"] = (
                f"/daily/sequence/media?t={token}&mid={item['id']}"
            )
        out.append(item)
    return out


def _option_stats(guild_id: int, today: str, mode: str, options: list) -> dict:
    """% de joueurs ayant choisi chaque proposition (pour le reveal).

    Renvoie {str(option_id): {"count": n, "pct": p}}. Dénominateur = total des
    tentatives du jour (un % faible = peu de monde s'est fait avoir ; un % élevé
    sur une mauvaise réponse = grosse "bait"). Clés en chaînes (snowflake)."""
    dist = database.get_guess_distribution(guild_id, today, mode=mode)
    total = sum(dist.values()) or 1
    stats = {}
    for opt in options:
        oid = opt[0]
        cnt = dist.get(oid, 0)
        stats[str(oid)] = {"count": cnt, "pct": round(100 * cnt / total)}
    return stats


def _attach_guess_labels(
    guild_id: int,
    options: list,
    results: list,
    mode: str,
) -> list:
    """Ajoute à chaque tentative FAUSSE le NOM de la personne devinée (Normal comme
    Hardcore) — site uniquement, jamais dans les embeds Discord. On affiche le nom
    complet (pas un numéro) pour éviter d'avoir à remonter voir les propositions.

    Les bonnes réponses n'ont pas d'étiquette (inutile). guess_label = nom (str)
    ou None."""
    name_by_id = {opt[0]: opt[1] for opt in options}
    for r in results:
        if r.get("correct"):
            r["guess_label"] = None
            continue
        gid = r.get("guessed_id")
        if mode == database.MODE_SEQUENCE:
            plural = "" if gid == 1 else "s"
            r["guess_label"] = f"{gid}/5 message{plural} bien placé{plural}"
            continue
        name = name_by_id.get(gid)
        if not name and gid:
            name = (database.get_user(guild_id, gid) or {}).get("name")
        r["guess_label"] = name or "—"
    return results


def _daily_progress_view(
    guild_id: int,
    date_str: str,
    viewer_user_id: int,
) -> list:
    """Progression des participants, personnalisée pour éviter tout spoil.

    Le résultat d'un mode n'est révélé que si le viewer a lui-même terminé ce
    mode. Avant cela, un résultat achevé est signalé sans indiquer victoire ou
    défaite. Le temps et le choix n'entrent dans le payload qu'après que le
    viewer a terminé le mode concerné.
    """
    presence = _active_presence(guild_id, date_str)
    attempts_by_mode = {}
    participants = {}

    for mode, _icon, _label in DAILY_MODE_SPECS:
        mode_attempts = {}
        for attempt in database.get_daily_results(guild_id, date_str, mode=mode):
            user_id = int(attempt["user_id"])
            mode_attempts[user_id] = attempt
            participants.setdefault(user_id, {
                "user_id": user_id,
                "name": attempt["user_name"],
            })
        attempts_by_mode[mode] = mode_attempts

    for user_id in presence:
        user = database.get_user(guild_id, user_id) or {}
        participants.setdefault(user_id, {
            "user_id": user_id,
            "name": user.get("name") or "Joueur",
        })

    viewer_completed = {
        mode
        for mode, attempts in attempts_by_mode.items()
        if viewer_user_id in attempts
    }
    option_labels_by_mode = {}
    for mode in viewer_completed:
        challenge = _load_challenge(guild_id, date_str, mode)
        if challenge is not None:
            option_labels_by_mode[mode] = {
                int(option[0]): option[1]
                for option in challenge["options"]
            }
    mode_labels = {mode: label for mode, _icon, label in DAILY_MODE_SPECS}
    viewer_shared = database.has_daily_result_share(
        guild_id,
        date_str,
        viewer_user_id,
    )
    out = []

    for user_id, participant in participants.items():
        active = presence.get(user_id)
        statuses = {}
        details = {}
        completed_count = 0
        playing_mode = None

        for mode, _icon, _label in DAILY_MODE_SPECS:
            attempt = attempts_by_mode[mode].get(user_id)
            if attempt is not None:
                completed_count += 1
                if mode in viewer_completed or user_id == viewer_user_id:
                    statuses[mode] = "win" if attempt["correct"] else "fail"
                    guessed_id = int(attempt.get("guessed_id") or 0)
                    guess_label = (
                        option_labels_by_mode.get(mode, {}).get(guessed_id)
                    )
                    if not guess_label and mode in (
                        database.MODE_AUTHOR,
                        database.MODE_MEDIA,
                    ):
                        guess_label = (
                            database.get_user(guild_id, guessed_id) or {}
                        ).get("name")
                    if mode == database.MODE_SEQUENCE:
                        plural = "" if guessed_id == 1 else "s"
                        guess_label = f"{guessed_id}/5 message{plural} bien placé{plural}"
                    details[mode] = {
                        "guess": guess_label or "Réponse inconnue",
                        "time": (
                            format_duration_ms(attempt.get("time_taken_ms"))
                            or "Temps inconnu"
                        ),
                        "score": guessed_id if mode == database.MODE_SEQUENCE else None,
                    }
                else:
                    statuses[mode] = "complete"
                continue

            is_current_mode = active is not None and active["mode"] == mode
            if is_current_mode and active.get("playing", False):
                statuses[mode] = "playing"
                playing_mode = mode
            else:
                statuses[mode] = "waiting"

        if playing_mode:
            activity = f"{mode_labels[playing_mode]} en cours"
        elif active and user_id in attempts_by_mode[active["mode"]]:
            activity = (
                "Daily terminé"
                if completed_count == len(DAILY_MODE_SPECS)
                else f"{mode_labels[active['mode']]} terminé"
            )
        elif active:
            activity = f"Sur {mode_labels[active['mode']]}"
        elif completed_count == len(DAILY_MODE_SPECS):
            activity = "Daily terminé"
        elif completed_count:
            plural = "s" if completed_count > 1 else ""
            activity = f"{completed_count} mode{plural} terminé{plural}"
        else:
            activity = "Daily ouvert"

        user = database.get_user(guild_id, user_id) or {}
        is_me = user_id == viewer_user_id
        daily_complete = completed_count == len(DAILY_MODE_SPECS)
        out.append({
            "user_id": str(user_id),
            "name": user.get("name") or participant["name"],
            "avatar_url": user.get("avatar_url") or default_avatar(user_id),
            "active": active is not None,
            "playing": playing_mode is not None,
            "activity": activity,
            "statuses": statuses,
            "details": details,
            "is_me": is_me,
            "can_share": is_me and daily_complete,
            "shared": is_me and viewer_shared,
            "_completed_count": completed_count,
        })

    out.sort(key=lambda player: (
        not player["playing"],
        not player["active"],
        -player["_completed_count"],
        player["name"].casefold(),
    ))
    for player in out:
        player.pop("_completed_count", None)
    return out


def _realtime_state(
    guild_id: int,
    date_str: str,
    mode: str,
    user_id: int,
    challenge: Optional[dict],
) -> dict:
    """État live personnalisé, commun à SSE, polling et POST /answer."""
    progress = _daily_progress_view(guild_id, date_str, user_id)
    has_attempt = database.get_daily_attempt(
        guild_id, date_str, user_id, mode=mode
    ) is not None
    if not has_attempt or challenge is None:
        return {
            "unlocked": False,
            "results": [],
            "leaderboard": [],
            "progress": progress,
            "participant_count": len(progress),
        }

    results = _enrich_results(
        database.get_daily_results(guild_id, date_str, mode=mode)
    )
    _attach_guess_labels(guild_id, challenge["options"], results, mode)
    results_view = [
        {
            "user_id": result["user_id"],
            "user_name": result["user_name"],
            "correct": result["correct"],
            "avatar_url": result["avatar_url"],
            "time_taken_str": result.get("time_taken_str"),
            "difficulty": result.get("difficulty", "normal"),
            "guess_label": result.get("guess_label"),
        }
        for result in results
    ]
    return {
        "unlocked": True,
        "results": results_view,
        "leaderboard": _leaderboard_view(
            guild_id,
            mode,
            user_id,
            date_str=date_str,
        ),
        "progress": progress,
        "participant_count": len(progress),
    }


def _reveal_options(guild_id: int, today: str, mode: str, options: list) -> list:
    """Propositions complètes (label + avatar + %) pour le reveal Hardcore.

    En Hardcore le joueur n'a jamais vu les 4 propositions ; au reveal il peut
    demander à les afficher (avec le % de votes). Hardcore = author/média, donc
    l'avatar est celui de l'auteur proposé."""
    stats = _option_stats(guild_id, today, mode, options)
    out = []
    for opt in options:
        oid = opt[0]
        out.append({
            "id": str(oid),
            "label": opt[1],
            "avatar_url": avatar_for_user(guild_id, oid),
            "pct": stats.get(str(oid), {}).get("pct"),
        })
    return out


def _mode_tabs(
    guild_id: int,
    today: str,
    user_id: int,
    user_name: str,
    user_avatar: str,
    active_mode: str,
    activity: bool = False,
) -> list:
    """Onglets de navigation entre les modes du daily."""
    tabs = []
    for mode, icon, label in DAILY_MODE_SPECS:
        available = _load_challenge(guild_id, today, mode) is not None
        tabs.append({
            "mode": mode,
            "icon": icon,
            "label": label,
            "active": mode == active_mode,
            "available": available,
            "url": _build_daily_link(
                guild_id,
                user_id,
                today,
                user_name,
                user_avatar,
                mode=mode,
                activity=activity,
            ) if available else "",
        })
    return tabs


def _admin_payload(token: str) -> Optional[dict]:
    """Valide un lien admin signé, limité au jour courant et à la whitelist."""
    payload = tokens.verify_token(token, config.WEBAPP_SECRET)
    if payload is None or payload.get("d") != today_str():
        return None
    try:
        user_id = int(payload["u"])
    except (KeyError, TypeError, ValueError):
        return None
    return payload if user_id in config.ADMIN_USER_IDS else None


# --- App ------------------------------------------------------------------

# --- Discord Activity (Embedded App SDK) ----------------------------------

# Dossier du build Vite de l'app Activity (généré par `npm run build` dans activity/).
_ACTIVITY_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "activity", "dist"
)

# En-tête pour autoriser l'embarquement dans l'iframe Discord.
_FRAME_ANCESTORS = (
    "frame-ancestors 'self' https://discord.com https://*.discord.com "
    "https://*.discordsays.com;"
)


def _exchange_oauth_code(code: str) -> Optional[str]:
    """Échange un code OAuth2 (reçu via l'Embedded App SDK) contre un access_token.

    Fait côté serveur uniquement : le client_secret ne quitte jamais le backend.
    Renvoie l'access_token, ou None si l'échange échoue."""
    data = urllib.parse.urlencode({
        "client_id": config.DISCORD_CLIENT_ID,
        "client_secret": config.DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://discord.com/api/oauth2/token",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # Obligatoire sinon Cloudflare bloque (erreur 1010).
            "User-Agent": "DiscordBot (https://github.com/Baach691/daily-guessr, 1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("access_token")


def _discord_oauth_error(err: urllib.error.HTTPError) -> dict:
    """Erreur lisible renvoyée par Discord pendant l'échange OAuth."""
    raw = err.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"raw": raw}
    payload.setdefault("status", err.code)
    return payload


def _discord_api_get(path: str, authorization: str):
    """Appel GET authentifié à l'API Discord, avec réponse JSON."""
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={
            "Authorization": authorization,
            "User-Agent": "DiscordBot (https://github.com/Baach691/daily-guessr, 1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _activity_member_has_allowed_role(guild_id: int, user_id: int) -> bool:
    """Applique la whitelist de rôles aux lancements depuis l'Activity."""
    allowed = config.ALLOWED_ROLE_IDS or []
    if not allowed:
        return True
    if not config.TOKEN:
        log.error("Activity: ALLOWED_ROLE_IDS défini mais DISCORD_TOKEN absent")
        return False
    try:
        member = _discord_api_get(
            f"/guilds/{guild_id}/members/{user_id}",
            f"Bot {config.TOKEN}",
        )
    except Exception:
        log.exception(
            "Activity: impossible de vérifier les rôles (guild=%s user=%s)",
            guild_id,
            user_id,
        )
        return False
    member_roles = {int(role_id) for role_id in member.get("roles", [])}
    return bool(member_roles.intersection(allowed))


def create_app(bot=None) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    # Les routes ne reçoivent que de petits payloads JSON. Cette limite coupe
    # court aux requêtes inutilement volumineuses avant même leur parsing.
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
    app.config["BOT"] = bot
    app.jinja_env.globals["default_avatar"] = default_avatar
    app.jinja_env.globals["streak_emoji"] = streak_emoji
    app.jinja_env.globals["loss_streak_emoji"] = loss_streak_emoji
    app.jinja_env.globals["format_duration_ms"] = format_duration_ms

    def asset_url(filename):
        """url_for static + ?v=<mtime> : force le navigateur à recharger le
        fichier après chaque déploiement (fini le JS/CSS périmé en cache)."""
        import os
        path = os.path.join(app.static_folder, filename)
        try:
            v = int(os.path.getmtime(path))
        except OSError:
            v = 0
        from flask import url_for as _url_for
        return f"{_url_for('static', filename=filename)}?v={v}"

    app.jinja_env.globals["asset_url"] = asset_url

    @app.after_request
    def _allow_discord_iframe(resp):
        # Autorise l'embarquement dans l'iframe Discord (Activity). N'affecte pas
        # l'usage en navigateur classique (frame-ancestors ne joue qu'en iframe).
        resp.headers["Content-Security-Policy"] = _FRAME_ANCESTORS
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        # Les liens Daily contiennent un jeton signé : ne jamais transmettre
        # l'URL complète comme Referer vers les avatars/CDN externes.
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Permissions-Policy", "camera=(), geolocation=(), microphone=()"
        )
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        if request.path.startswith(("/api/", "/daily", "/phrase", "/media", "/admin")):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # --- Activity : échange OAuth2 (code -> access_token) -------------------
    # Routes doublées avec le préfixe /.proxy/ : dans l'iframe Discord, les requêtes
    # passent par le proxy discordsays.com et peuvent arriver préfixées /.proxy/.
    @app.route("/api/token", methods=["POST"])
    @app.route("/.proxy/api/token", methods=["POST"])
    def activity_token():
        data = request.get_json(silent=True) or {}
        code = data.get("code")
        if not isinstance(code, str) or not 1 <= len(code) <= 2048:
            return jsonify({"error": "missing_code"}), 400
        if not config.DISCORD_CLIENT_ID or not config.DISCORD_CLIENT_SECRET:
            log.error("Activity: VITE_DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET absents du .env")
            return jsonify({"error": "server_not_configured"}), 500
        try:
            access_token = _exchange_oauth_code(code)
        except urllib.error.HTTPError as e:
            details = _discord_oauth_error(e)
            log.error("Activity: Discord a refusé l'échange OAuth2: %s", details)
            return jsonify({"error": "discord_token_exchange_failed", "details": details}), 502
        except Exception:
            log.exception("Activity: échec de l'échange du code OAuth2")
            return jsonify({"error": "token_exchange_failed"}), 502
        if not access_token:
            return jsonify({"error": "no_access_token"}), 502
        return jsonify({"access_token": access_token})

    @app.route("/api/activity/session", methods=["POST"])
    @app.route("/.proxy/api/activity/session", methods=["POST"])
    def activity_session():
        """Transforme l'identité OAuth Discord en lien Daily signé.

        Le guild_id reçu du SDK n'est jamais cru seul : le token OAuth doit
        appartenir à cette application et l'utilisateur doit être membre du
        serveur demandé.
        """
        data = request.get_json(silent=True) or {}
        access_token = data.get("access_token")
        guild_id_raw = data.get("guild_id")
        if (
            not isinstance(access_token, str)
            or not 1 <= len(access_token) <= 4096
        ):
            return jsonify({"error": "missing_access_token"}), 400
        try:
            guild_id = int(guild_id_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "activity_requires_guild"}), 400

        try:
            authorization = _discord_api_get("/oauth2/@me", f"Bearer {access_token}")
            user = authorization.get("user") or _discord_api_get(
                "/users/@me", f"Bearer {access_token}"
            )
            guilds = _discord_api_get("/users/@me/guilds", f"Bearer {access_token}")
        except urllib.error.HTTPError as e:
            log.warning(
                "Activity: token OAuth refusé pendant la création de session: %s",
                _discord_oauth_error(e),
            )
            return jsonify({"error": "invalid_discord_session"}), 401
        except Exception:
            log.exception("Activity: échec de validation de la session Discord")
            return jsonify({"error": "discord_session_unavailable"}), 502

        application_id = str((authorization.get("application") or {}).get("id", ""))
        if application_id != str(config.DISCORD_CLIENT_ID):
            return jsonify({"error": "wrong_discord_application"}), 403
        if not any(str(guild.get("id")) == str(guild_id) for guild in guilds):
            return jsonify({"error": "not_a_guild_member"}), 403

        try:
            user_id = int(user["id"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "invalid_discord_user"}), 502
        if not _activity_member_has_allowed_role(guild_id, user_id):
            return jsonify({"error": "role_not_allowed"}), 403

        user_name = user.get("global_name") or user.get("username") or "Joueur"
        avatar_hash = user.get("avatar")
        user_avatar = (
            f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=128"
            if avatar_hash else default_avatar(user_id)
        )
        database.upsert_user(guild_id, user_id, user_name, user_avatar)

        today = today_str()
        payload = {
            "g": guild_id,
            "u": user_id,
            "d": today,
            "n": user_name,
            "a": user_avatar,
            "x": "activity",
        }
        token = tokens.make_token(payload, config.WEBAPP_SECRET)
        return jsonify({"url": f"/daily?t={token}"})

    # --- Activity : sert l'app embarquée (build Vite) à la racine -----------
    @app.route("/")
    @app.route("/.proxy")
    @app.route("/.proxy/")
    def activity_root():
        index_html = os.path.join(_ACTIVITY_DIST, "index.html")
        if not os.path.exists(index_html):
            return render_template(
                "error.html", title="Activity pas encore buildée",
                message="Lance `npm install && npm run build` dans le dossier activity/.",
            ), 200
        return send_from_directory(_ACTIVITY_DIST, "index.html")

    @app.route("/assets/<path:filename>")
    @app.route("/.proxy/assets/<path:filename>")
    def activity_assets(filename):
        # Discord conserve parfois le préfixe /.proxy/ avec l'URL Override.
        return send_from_directory(os.path.join(_ACTIVITY_DIST, "assets"), filename)

    @app.route("/daily")
    @app.route("/.proxy/daily")
    @app.route("/phrase")
    @app.route("/media")
    def daily_page():
        token = request.args.get("t", "")
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        if payload is None:
            return (
                render_template(
                    "error.html", title="Lien invalide",
                    message="Ce lien n'est pas valide. Refais la commande dans Discord.",
                ),
                403,
            )
        today = today_str()
        if payload.get("d") != today:
            return (
                render_template(
                    "error.html", title="Lien expiré",
                    message="Ce lien est d'un autre jour. Relance la commande dans Discord.",
                ),
                410,
            )

        mode = _payload_mode(payload)
        is_activity = payload.get("x") == "activity"
        guild_id = payload["g"]
        user_id = payload["u"]
        user_name = payload.get("n", "Joueur")
        user_avatar = payload.get("a") or default_avatar(user_id)
        database.upsert_user(guild_id, user_id, user_name, user_avatar)

        ch = _load_challenge(guild_id, today, mode)
        if ch is None:
            return (
                render_template(
                    "error.html", title="Pas de défi du jour",
                    message="Aucun défi du jour pour ce serveur. Relance la commande dans Discord.",
                ),
                404,
            )

        attempt = database.get_daily_attempt(guild_id, today, user_id, mode=mode)
        current_streak, best_streak = database.get_streak(
            guild_id, user_id, today_str=today, mode=mode
        )
        results = database.get_daily_results(guild_id, today, mode=mode)

        # Hardcore : disponible sur "Qui a écrit ça ?" ET "Devine le média" (réponse =
        # un auteur dans les modes concernés). Verrou de difficulté (par mode) une fois
        # "Jouer" cliqué (sinon le joueur reste libre de choisir).
        hardcore_enabled = mode in (database.MODE_AUTHOR, database.MODE_MEDIA)
        stored_difficulty = database.get_daily_difficulty(
            guild_id, today, user_id, mode
        )
        locked_difficulty = stored_difficulty if hardcore_enabled else None
        hardcore_base_seconds = _hardcore_base_seconds(mode)
        hardcore_limit_ms = int(round(1000 * _hardcore_limit_seconds(
            guild_id, today, user_id, mode
        )))
        if _touch_presence(
            guild_id,
            today,
            user_id,
            mode,
            playing=attempt is None and stored_difficulty is not None,
        ):
            _publish_realtime((guild_id, today))
        initial_realtime_state = _realtime_state(
            guild_id,
            today,
            mode,
            user_id,
            ch,
        )

        # Options + (si déjà joué) le % de joueurs ayant choisi chacune (reveal).
        options_view = _options_view(guild_id, ch["options"], mode)
        if attempt is not None:
            stats = _option_stats(guild_id, today, mode, ch["options"])
            for o in options_view:
                o["pct"] = stats.get(str(o["id"]), {}).get("pct")
        sequence_messages = []
        sequence_correct_messages = []
        if mode == database.MODE_SEQUENCE:
            if attempt is None:
                sequence_messages = _sequence_messages_view(
                    guild_id,
                    today,
                    user_id,
                    token,
                    ch,
                    shuffled=True,
                )
            else:
                sequence_messages = _sequence_messages_view(
                    guild_id,
                    today,
                    user_id,
                    token,
                    ch,
                    order_ids=attempt.get("guessed_order") or [],
                )
                sequence_correct_messages = _sequence_messages_view(
                    guild_id,
                    today,
                    user_id,
                    token,
                    ch,
                )

        _titles = {
            database.MODE_PHRASE: "Devine la phrase",
            database.MODE_MEDIA: "Devine le média",
            database.MODE_SEQUENCE: "Remets la conversation dans l'ordre",
        }
        return render_template(
            "daily.html",
            mode=mode,
            is_phrase=(mode == database.MODE_PHRASE),
            is_media=ch.get("is_media", False),
            is_sequence=ch.get("is_sequence", False),
            sequence_messages=sequence_messages,
            sequence_correct_messages=sequence_correct_messages,
            media_url=(
                f"/daily/media?t={token}" if ch.get("is_media", False) else ""
            ),
            media_view_url=(
                f"{config.WEBAPP_BASE_URL.rstrip('/')}/daily/media/view?t={token}"
                if ch.get("is_media", False) else ""
            ),
            media_is_video=ch.get("media_is_video", False),
            media_is_gif=ch.get("media_is_gif", False),
            page_title=_titles.get(mode, "Qui a écrit ça ?"),
            hardcore_enabled=hardcore_enabled,
            hardcore_seconds=hardcore_base_seconds,
            hardcore_limit_ms=hardcore_limit_ms,
            media_hardcore_max_ms=_MEDIA_HARDCORE_MAX_SECONDS * 1000,
            locked_difficulty=locked_difficulty,
            subject_name=ch["subject_name"],
            subject_avatar_url=ch["subject_avatar_url"],
            date=today,
            date_display=format_date_fr(today),
            message_content=ch["message_content"],
            options=options_view,
            correct_author_id=ch["correct_id"],
            correct_author_name=ch["correct_name"],
            attempt=attempt,
            current_streak=current_streak,
            best_streak=best_streak,
            total_count=len(results),
            # Anti-triche : tant que le joueur n'a pas joué, on ne lui livre NI le
            # classement live NI le compteur (les streaks/le nombre de joueurs
            # laissent deviner si le défi est piégeux). Rempli par le JS après
            # la réponse. On n'envoie donc rien dans le HTML source avant de jouer.
            leaderboard=(
                _leaderboard_view(
                    guild_id,
                    mode,
                    user_id,
                    date_str=today,
                )
                if attempt
                else []
            ),
            leaderboard_hidden=False,
            mode_tabs=_mode_tabs(
                guild_id,
                today,
                user_id,
                user_name,
                user_avatar,
                mode,
                activity=is_activity,
            ),
            is_admin=user_id in config.ADMIN_USER_IDS,
            admin_url=f"/admin?t={token}",
            token=token,
            user_id=user_id,
            user_name=user_name,
            user_avatar=user_avatar,
            is_activity=is_activity,
            initial_realtime_state=initial_realtime_state,
        )

    @app.route("/daily/media/view")
    def daily_media_view():
        """Lecteur externe sans auteur ni lien vers le message Discord."""
        token = request.args.get("t", "")
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        if payload is None or payload.get("d") != today_str():
            return render_template(
                "error.html",
                title="Lien invalide",
                message="Ce lecteur n'est plus disponible.",
            ), 403
        if _payload_mode(payload) != database.MODE_MEDIA:
            return render_template(
                "error.html",
                title="Lien invalide",
                message="Ce lien ne correspond pas à un média.",
            ), 403

        guild_id = int(payload["g"])
        daily = database.get_daily(
            guild_id, today_str(), mode=database.MODE_MEDIA
        )
        if daily is None:
            return render_template(
                "error.html",
                title="Média indisponible",
                message="Le média du jour est introuvable.",
            ), 404

        response = render_template(
            "media_view.html",
            media_url=f"/daily/media?t={token}",
            media_is_video=_is_video_url(daily["content"]),
        )
        response = current_app.make_response(response)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/daily/media")
    def daily_media():
        """Diffuse le média du jour via le serveur avec une URL Discord fraîche.

        Les URLs CDN de pièces jointes Discord sont signées et expirent. Le
        message reste disponible, mais une URL stockée en base finit par afficher
        « This content is no longer available ». On relit donc le message puis
        on transmet les octets depuis notre origine.
        """
        token = request.args.get("t", "")
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        if payload is None or payload.get("d") != today_str():
            return jsonify({"error": "invalid_token"}), 403
        if _payload_mode(payload) != database.MODE_MEDIA:
            return jsonify({"error": "not_media_mode"}), 403

        guild_id = int(payload["g"])
        daily = database.get_daily(
            guild_id, today_str(), mode=database.MODE_MEDIA
        )
        if daily is None:
            return jsonify({"error": "no_daily"}), 404

        return _serve_discord_media(
            daily["channel_id"],
            daily["message_id"],
            daily["content"],
        )

    @app.route("/daily/sequence/media")
    def daily_sequence_media():
        """Diffuse une pièce jointe appartenant aux cinq messages du défi."""
        token = request.args.get("t", "")
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        if payload is None or payload.get("d") != today_str():
            return jsonify({"error": "invalid_token"}), 403
        if _payload_mode(payload) != database.MODE_SEQUENCE:
            return jsonify({"error": "not_sequence_mode"}), 403
        try:
            message_id = int(request.args.get("mid", ""))
        except (TypeError, ValueError):
            return jsonify({"error": "bad_message_id"}), 400

        daily = database.get_sequence_daily(int(payload["g"]), today_str())
        if daily is None:
            return jsonify({"error": "no_daily"}), 404
        message = next(
            (
                item
                for item in daily["messages"]
                if int(item["id"]) == message_id and item.get("has_media")
            ),
            None,
        )
        if message is None:
            return jsonify({"error": "media_not_in_daily"}), 404
        return _serve_discord_media(
            daily["channel_id"],
            message_id,
            message.get("media_url", ""),
        )

    def _serve_discord_media(
        channel_id: int,
        message_id: int,
        fallback_url: str,
    ):
        """Proxy borné d'une pièce jointe Discord avec support des vidéos Range."""
        bot = current_app.config.get("BOT")
        media_url = fetch_current_media_url(
            bot, channel_id, message_id
        ) or fallback_url
        if not _is_discord_attachment_url(media_url):
            log.error("URL média Discord refusée: %r", media_url)
            return jsonify({"error": "invalid_media_url"}), 502

        headers = {
            "User-Agent": "DiscordBot (https://github.com/Baach691/daily-guessr, 1.0)"
        }
        range_header = request.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        upstream_request = urllib.request.Request(media_url, headers=headers)
        try:
            upstream = urllib.request.urlopen(upstream_request, timeout=15)
        except urllib.error.HTTPError as exc:
            log.warning(
                "Discord CDN refuse le média %s/%s: HTTP %s",
                channel_id,
                message_id,
                exc.code,
            )
            return jsonify({"error": "media_unavailable"}), 502
        except Exception:
            log.exception("Échec du chargement du média Discord")
            return jsonify({"error": "media_unavailable"}), 502

        response_headers = {
            "Content-Type": upstream.headers.get(
                "Content-Type", "application/octet-stream"
            ),
            "Cache-Control": "private, max-age=300",
        }
        for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
            value = upstream.headers.get(header)
            if value:
                response_headers[header] = value

        def generate():
            try:
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate()),
            status=getattr(upstream, "status", 200),
            headers=response_headers,
        )

    @app.route("/admin")
    def admin_page():
        token = request.args.get("t", "")
        payload = _admin_payload(token)
        if payload is None:
            return (
                render_template(
                    "error.html",
                    title="Accès refusé",
                    message="Ce panneau est réservé aux administrateurs autorisés.",
                ),
                403,
            )

        guild_id = int(payload["g"])
        user_id = int(payload["u"])
        dates = database.get_daily_dates(guild_id)
        selected_date = request.args.get("date", "")
        if selected_date not in dates:
            selected_date = dates[0] if dates else ""

        mode = request.args.get("mode", database.MODE_AUTHOR)
        if mode not in database.VALID_MODES:
            mode = database.MODE_AUTHOR

        mode_specs = (
            (database.MODE_AUTHOR, "🌞", "Qui a écrit ça ?"),
            (database.MODE_PHRASE, "✍️", "Devine la phrase"),
            (database.MODE_MEDIA, "🖼️", "Devine le média"),
            (database.MODE_SEQUENCE, "🔀", "Remets dans l'ordre"),
        )
        mode_tabs = []
        for tab_mode, icon, label in mode_specs:
            available = bool(
                selected_date
                and _load_challenge(guild_id, selected_date, tab_mode) is not None
            )
            mode_tabs.append({
                "mode": tab_mode,
                "icon": icon,
                "label": label,
                "active": tab_mode == mode,
                "available": available,
                "url": (
                    f"/admin?t={token}&date={selected_date}&mode={tab_mode}"
                    if available else ""
                ),
            })

        challenge = (
            _load_challenge(guild_id, selected_date, mode)
            if selected_date else None
        )
        attempts = (
            database.get_daily_results(guild_id, selected_date, mode=mode)
            if challenge else []
        )
        options = []
        option_ids = set()
        if challenge:
            options = _options_view(guild_id, challenge["options"], mode)
            option_ids = {int(option["id"]) for option in options}
        for attempt in attempts:
            attempt["avatar_url"] = (
                attempt.get("avatar_url") or default_avatar(attempt["user_id"])
            )
            attempt["has_known_guess"] = int(attempt["guessed_id"]) in option_ids

        active_mode = _payload_mode(payload)
        back_url = _build_daily_link(
            guild_id,
            user_id,
            today_str(),
            payload.get("n", "Joueur"),
            payload.get("a") or default_avatar(user_id),
            mode=active_mode,
            activity=payload.get("x") == "activity",
        )
        return render_template(
            "admin.html",
            token=token,
            dates=dates,
            selected_date=selected_date,
            mode=mode,
            mode_tabs=mode_tabs,
            challenge=challenge,
            attempts=attempts,
            options=options,
            back_url=back_url,
            saved=request.args.get("saved"),
        )

    @app.route("/admin/correct", methods=["POST"])
    def admin_correct():
        data = request.get_json(silent=True) or {}
        payload = _admin_payload(data.get("token", ""))
        if payload is None:
            return jsonify({"error": "admin_forbidden"}), 403

        mode = data.get("mode", "")
        date_str = data.get("date", "")
        if mode not in database.VALID_MODES:
            return jsonify({"error": "bad_mode"}), 400
        guild_id = int(payload["g"])
        if date_str not in database.get_daily_dates(guild_id):
            return jsonify({"error": "bad_date"}), 400
        try:
            target_user_id = int(data["user_id"])
            guessed_id = int(data["guessed_id"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "bad_correction"}), 400

        challenge = _load_challenge(guild_id, date_str, mode)
        if challenge is None:
            return jsonify({"error": "no_daily"}), 404
        allowed_guesses = {int(option[0]) for option in challenge["options"]}
        allowed_guesses.add(0)
        current_attempt = database.get_daily_attempt(
            guild_id, date_str, target_user_id, mode=mode
        )
        if current_attempt is not None:
            allowed_guesses.add(int(current_attempt["guessed_id"]))
        if guessed_id not in allowed_guesses:
            return jsonify({"error": "guess_not_in_daily"}), 400

        result = database.correct_daily_attempt(
            guild_id,
            date_str,
            target_user_id,
            guessed_id,
            mode=mode,
        )
        if result is None:
            return jsonify({"error": "attempt_not_found"}), 404

        log.warning(
            "Correction admin=%s guild=%s date=%s mode=%s user=%s: %s/%s -> %s/%s",
            payload["u"],
            guild_id,
            date_str,
            mode,
            target_user_id,
            result["old_guessed_id"],
            result["old_correct"],
            result["guessed_id"],
            result["correct"],
        )
        _publish_realtime((guild_id, date_str))
        return jsonify(result)

    def _realtime_viewer(token: str):
        """Valide un viewer du flux live et charge son mode courant."""
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        if payload is None:
            return None, None, (jsonify({"error": "invalid_token"}), 403)
        date_str = today_str()
        if payload.get("d") != date_str:
            return None, None, (jsonify({"error": "expired_token"}), 410)

        mode = _payload_mode(payload)
        guild_id = int(payload["g"])
        user_id = int(payload["u"])
        challenge = _load_challenge(guild_id, date_str, mode)
        if challenge is None:
            return None, None, (jsonify({"error": "no_daily"}), 404)
        context = {
            "guild_id": guild_id,
            "date": date_str,
            "mode": mode,
            "user_id": user_id,
        }
        if _touch_presence(guild_id, date_str, user_id, mode):
            _publish_realtime((guild_id, date_str))
        return context, challenge, None

    @app.route("/daily/state")
    @app.route("/.proxy/daily/state")
    def daily_state():
        """État ponctuel utilisé comme fallback si le proxy bloque le SSE."""
        context, challenge, error = _realtime_viewer(request.args.get("t", ""))
        if error is not None:
            return error
        response = jsonify(_realtime_state(
            context["guild_id"],
            context["date"],
            context["mode"],
            context["user_id"],
            challenge,
        ))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/daily/presence", methods=["POST"])
    @app.route("/.proxy/daily/presence", methods=["POST"])
    def daily_presence():
        """Heartbeat court utilisé pour retirer les joueurs ayant quitté l'Activity."""
        data = request.get_json(silent=True) or {}
        context, _challenge, error = _realtime_viewer(data.get("token", ""))
        if error is not None:
            return error
        return jsonify({"ok": True})

    @app.route("/daily/share", methods=["POST"])
    @app.route("/.proxy/daily/share", methods=["POST"])
    def daily_share():
        """Publie une fois le bilan emoji du joueur dans le salon du ping."""
        data = request.get_json(silent=True) or {}
        payload = tokens.verify_token(data.get("token", ""), config.WEBAPP_SECRET)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 403
        date_str = today_str()
        if payload.get("d") != date_str:
            return jsonify({"error": "expired_token"}), 410

        guild_id = int(payload["g"])
        user_id = int(payload["u"])
        attempts = {
            mode: database.get_daily_attempt(
                guild_id,
                date_str,
                user_id,
                mode=mode,
            )
            for mode in database.VALID_MODES
        }
        if any(attempt is None for attempt in attempts.values()):
            return jsonify({"error": "daily_not_complete"}), 409
        if database.has_daily_result_share(guild_id, date_str, user_id):
            return jsonify({"error": "already_shared", "shared": True}), 409

        channel_id = _daily_result_channel_id(guild_id, date_str)
        if channel_id is None:
            return jsonify({"error": "share_channel_unavailable"}), 503
        if not database.reserve_daily_result_share(
            guild_id,
            date_str,
            user_id,
            channel_id,
        ):
            if database.has_daily_result_share(guild_id, date_str, user_id):
                return jsonify({"error": "already_shared", "shared": True}), 409
            return jsonify({"error": "share_in_progress"}), 409

        user_name = (
            attempts[database.MODE_AUTHOR].get("user_name")
            or payload.get("n")
            or "Joueur"
        )
        content = _format_daily_result_share(date_str, user_name, attempts)
        message_id = send_daily_result(
            current_app.config.get("BOT"),
            channel_id,
            content,
        )
        if message_id is None:
            database.cancel_daily_result_share(guild_id, date_str, user_id)
            return jsonify({"error": "share_failed"}), 502

        database.complete_daily_result_share(
            guild_id,
            date_str,
            user_id,
            message_id,
        )
        _publish_realtime((guild_id, date_str))
        return jsonify({"ok": True, "shared": True})

    @app.route("/daily/stream")
    @app.route("/.proxy/daily/stream")
    def daily_stream():
        """Flux SSE du daily entier, personnalisé selon les modes déjà joués."""
        context, challenge, error = _realtime_viewer(request.args.get("t", ""))
        if error is not None:
            return error
        key = (context["guild_id"], context["date"])

        def generate():
            subscriber = _subscribe_realtime(key)
            try:
                state = _realtime_state(
                    context["guild_id"],
                    context["date"],
                    context["mode"],
                    context["user_id"],
                    challenge,
                )
                yield (
                    "data: "
                    + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                    + "\n\n"
                )
                while True:
                    try:
                        subscriber.get(timeout=15)
                    except queue.Empty:
                        pass
                    state = _realtime_state(
                        context["guild_id"],
                        context["date"],
                        context["mode"],
                        context["user_id"],
                        challenge,
                    )
                    yield (
                        "data: "
                        + json.dumps(
                            state,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n\n"
                    )
            finally:
                _unsubscribe_realtime(key, subscriber)

        response = Response(
            stream_with_context(generate()),
            content_type="text/event-stream; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-cache, no-store"
        response.headers["X-Accel-Buffering"] = "no"
        response.headers["Connection"] = "keep-alive"
        return response

    @app.route("/daily/answer", methods=["POST"])
    @app.route("/phrase/answer", methods=["POST"])
    @app.route("/media/answer", methods=["POST"])
    def daily_answer():
        data = request.get_json(silent=True) or {}
        token = data.get("token", "")
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 403
        today = today_str()
        if payload.get("d") != today:
            return jsonify({"error": "expired_token"}), 410

        mode = _payload_mode(payload)
        guild_id = payload["g"]
        user_id = payload["u"]
        user_name = payload.get("n", "Joueur")
        user_avatar = payload.get("a") or default_avatar(user_id)
        database.upsert_user(guild_id, user_id, user_name, user_avatar)

        ch = _load_challenge(guild_id, today, mode)
        if ch is None:
            return jsonify({"error": "no_daily"}), 404

        # Difficulté = celle verrouillée au clic "Jouer" (Hardcore = Qui a écrit ça ?
        # + Devine le média). Lue côté serveur → impossible de tricher.
        if mode in (database.MODE_AUTHOR, database.MODE_MEDIA):
            difficulty = (
                database.get_daily_difficulty(guild_id, today, user_id, mode) or "normal"
            )
        else:
            difficulty = "normal"

        resolved_name = None
        raw_guessed_id = data.get("guessed_id")
        guess_text = (data.get("guess_text") or "").strip()
        guessed_order = []
        if mode == database.MODE_SEQUENCE:
            raw_order = data.get("guess_order")
            if not isinstance(raw_order, list) or len(raw_order) != 5:
                return jsonify({"error": "bad_sequence_order"}), 400
            try:
                guessed_order = [int(message_id) for message_id in raw_order]
                correct_order = [
                    int(message["id"]) for message in ch["sequence_messages"]
                ]
            except (TypeError, ValueError):
                return jsonify({"error": "bad_sequence_order"}), 400
            if len(set(guessed_order)) != 5 or set(guessed_order) != set(correct_order):
                return jsonify({"error": "bad_sequence_order"}), 400
            guessed_id = sum(
                guessed == correct
                for guessed, correct in zip(guessed_order, correct_order)
            )
        elif difficulty == "hardcore":
            # Cas normal : le joueur a SÉLECTIONNÉ un membre dans la liste → on
            # reçoit directement son id (ce qu'il voit = ce qu'il envoie).
            if raw_guessed_id not in (None, "", 0, "0"):
                try:
                    guessed_id = int(raw_guessed_id)
                except (TypeError, ValueError):
                    return jsonify({"error": "bad_guessed_id"}), 400
                u = database.get_user(guild_id, guessed_id) if guessed_id else None
                resolved_name = (u or {}).get("name")
            # Repli : saisie libre non sélectionnée → résolution floue (typos/surnoms).
            elif guess_text:
                match = database.resolve_member_guess(guild_id, guess_text)
                if match is not None:
                    guessed_id, resolved_name = match
                else:
                    guessed_id = 0  # saisie non reliée à un membre → réponse fausse
            else:
                guessed_id = 0
        else:
            # Mode normal : id de l'option cliquée.
            guessed_id = raw_guessed_id
            if guessed_id is None:
                return jsonify({"error": "missing_guessed_id"}), 400
            try:
                guessed_id = int(guessed_id)
            except (TypeError, ValueError):
                return jsonify({"error": "bad_guessed_id"}), 400

        is_correct = guessed_id == ch["correct_id"]

        # Temps de réponse : on IGNORE la valeur du client (truquable) et on la
        # recalcule depuis l'heure de départ stockée au "Jouer" (heure serveur).
        # Fallback sur le client seulement si aucun départ n'a été enregistré
        # (ex: échec réseau du /start), pour ne pas perdre le temps complètement.
        server_elapsed = database.get_start_elapsed_seconds(
            guild_id, today, user_id, mode
        )
        if server_elapsed is not None:
            time_taken_ms = int(round(server_elapsed * 1000))
        else:
            time_taken_ms = data.get("time_taken_ms")
            if time_taken_ms is not None:
                try:
                    time_taken_ms = int(time_taken_ms)
                except (TypeError, ValueError):
                    time_taken_ms = None

        # Hardcore : limite de temps. Arbitre serveur (heure de démarrage stockée
        # par mode) → on ne peut pas tricher en envoyant un faux time_taken_ms.
        # Marge de 3 s pour absorber la latence réseau du dernier clic.
        timed_out = False
        if difficulty == "hardcore":
            elapsed = database.get_start_elapsed_seconds(guild_id, today, user_id, mode)
            allowed_seconds = _hardcore_limit_seconds(
                guild_id, today, user_id, mode
            )
            if elapsed is not None and elapsed > allowed_seconds + 3:
                timed_out = True
                is_correct = False  # au-delà du temps imparti = défaite

        points = 2 if difficulty == "hardcore" else 1

        if mode == database.MODE_SEQUENCE:
            ok = database.record_sequence_attempt(
                guild_id,
                today,
                user_id,
                user_name,
                guessed_order,
                guessed_id,
                is_correct,
                time_taken_ms=time_taken_ms,
            )
        else:
            ok = database.record_daily_attempt(
                guild_id, today, user_id, user_name, guessed_id, is_correct,
                time_taken_ms=time_taken_ms, mode=mode, difficulty=difficulty,
            )
        if not ok:
            return jsonify({"error": "already_answered"}), 409

        current_streak, best_streak = database.update_streak(
            guild_id, user_id, today, is_correct, mode=mode
        )
        database.record_answer(
            guild_id,
            user_id,
            user_name,
            is_correct,
            mode=mode,
            points=points,
            earned_points=(guessed_id if mode == database.MODE_SEQUENCE else None),
        )

        _touch_presence(guild_id, today, user_id, mode, playing=False)
        state = _realtime_state(guild_id, today, mode, user_id, ch)
        correct_count = sum(1 for result in state["results"] if result["correct"])
        _publish_realtime((guild_id, today))

        # Mode phrase : on révèle l'avatar de l'auteur de chaque phrase APRÈS la
        # réponse (option_id -> avatar). Pas exposé avant (anti-spoil).
        option_avatars = {}
        if mode == database.MODE_PHRASE:
            for opt in ch["options"]:
                if len(opt) > 2:
                    option_avatars[str(opt[0])] = avatar_for_user(guild_id, opt[2])

        return jsonify({
            "correct": is_correct,
            "correct_id": str(ch["correct_id"]),  # str : précision snowflake côté JS
            "correct_name": ch["correct_name"],
            "difficulty": difficulty,
            "resolved_name": resolved_name,
            "guessed_id": str(guessed_id),
            "correct_order": (
                [str(message["id"]) for message in ch["sequence_messages"]]
                if mode == database.MODE_SEQUENCE
                else []
            ),
            "option_avatars": option_avatars,
            "option_stats": _option_stats(guild_id, today, mode, ch["options"]),
            # Hardcore : propositions complètes pour le bouton "voir le mode Normal".
            "reveal_options": (
                _reveal_options(guild_id, today, mode, ch["options"])
                if difficulty == "hardcore" else []
            ),
            "timed_out": timed_out,
            "points_awarded": (
                guessed_id
                if mode == database.MODE_SEQUENCE
                else points if is_correct else 0
            ),
            "current_streak": current_streak,
            "best_streak": best_streak,
            "results": state["results"],
            "stats": {"correct": correct_count, "total": len(state["results"])},
            "leaderboard": state["leaderboard"],
            "progress": state["progress"],
            "participant_count": state["participant_count"],
        })

    @app.route("/daily/start", methods=["POST"])
    @app.route("/phrase/start", methods=["POST"])
    @app.route("/media/start", methods=["POST"])
    def daily_start():
        """Verrouille la difficulté au clic "Jouer". Renvoie la difficulté effective.

        Hardcore en "qui a écrit ça ?" et "devine le média" ; ailleurs toujours normal.
        """
        data = request.get_json(silent=True) or {}
        payload = tokens.verify_token(data.get("token", ""), config.WEBAPP_SECRET)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 403
        today = today_str()
        if payload.get("d") != today:
            return jsonify({"error": "expired_token"}), 410

        mode = _payload_mode(payload)
        guild_id = payload["g"]
        user_id = payload["u"]

        wanted = data.get("difficulty", "normal")
        if mode not in (database.MODE_AUTHOR, database.MODE_MEDIA) or wanted not in (
            "normal", "hardcore"
        ):
            wanted = "normal"

        time_bonus_seconds = 0.0
        if mode == database.MODE_MEDIA and wanted == "hardcore":
            challenge = _load_challenge(guild_id, today, mode)
            raw_duration_ms = data.get("media_duration_ms")
            try:
                duration_seconds = float(raw_duration_ms) / 1000
            except (TypeError, ValueError):
                duration_seconds = 0.0
            max_bonus = (
                _MEDIA_HARDCORE_MAX_SECONDS
                - _MEDIA_HARDCORE_BASE_SECONDS
            )
            if challenge is not None and challenge.get("media_is_gif"):
                time_bonus_seconds = _MEDIA_HARDCORE_GIF_BONUS_SECONDS
            elif (
                challenge is not None
                and challenge.get("media_is_video")
                and math.isfinite(duration_seconds)
            ):
                time_bonus_seconds = min(
                    max(0.0, duration_seconds),
                    max_bonus,
                )

        # Enregistre l'heure de départ serveur (non-truquable) ET la difficulté,
        # par mode. Le premier "Jouer" gagne (immuable). Renvoie l'effective.
        effective = database.set_daily_start(
            guild_id,
            today,
            user_id,
            mode,
            difficulty=wanted,
            time_bonus_seconds=time_bonus_seconds,
        )
        effective_limit_ms = int(round(1000 * _hardcore_limit_seconds(
            guild_id, today, user_id, mode
        )))
        _touch_presence(guild_id, today, user_id, mode, playing=True)
        _publish_realtime((guild_id, today))
        return jsonify({
            "difficulty": effective,
            "hardcore_limit_ms": effective_limit_ms,
        })

    @app.route("/daily/options")
    @app.route("/phrase/options")
    @app.route("/media/options")
    def daily_options():
        """Renvoie les propositions du jour, UNIQUEMENT pour un joueur en Normal.

        Anti-triche Hardcore : si le joueur a verrouillé Hardcore, on ne lui livre
        jamais les propositions (sinon il connaîtrait les 4 réponses possibles).
        """
        payload = tokens.verify_token(request.args.get("t", ""), config.WEBAPP_SECRET)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 403
        today = today_str()
        if payload.get("d") != today:
            return jsonify({"error": "expired_token"}), 410

        mode = _payload_mode(payload)
        guild_id = payload["g"]
        user_id = payload["u"]

        # Modes avec Hardcore (author + média) : il faut être verrouillé Normal
        # pour recevoir les propositions (sinon le Hardcore connaîtrait les 4
        # réponses). Phrase : pas de Hardcore → toujours servi.
        if mode in (database.MODE_AUTHOR, database.MODE_MEDIA):
            if database.get_daily_difficulty(guild_id, today, user_id, mode) != "normal":
                return jsonify({"options": []})

        ch = _load_challenge(guild_id, today, mode)
        if ch is None:
            return jsonify({"error": "no_daily"}), 404
        # IDs en chaînes : les snowflakes Discord dépassent la précision des
        # nombres JS (2^53) ; en JSON-number ils seraient arrondis côté client.
        opts = _options_view(guild_id, ch["options"], mode)
        for o in opts:
            o["id"] = str(o["id"])
            # Anti-spoil phrase : on n'expose PAS l'avatar de l'auteur avant la
            # réponse (sinon visible dans l'onglet réseau). Révélé via /answer.
            if o.get("reveal_only"):
                o["avatar_url"] = ""
        return jsonify({"options": opts})

    @app.route("/daily/search")
    @app.route("/phrase/search")
    @app.route("/media/search")
    def daily_search():
        """Suggestions Hardcore au fil de la frappe (autocomplétion).

        Vide → aucune suggestion (on ne souffle pas la liste). Disponible
        uniquement pendant une partie Hardcore (author ou média, verrou hardcore) :
        le but est d'aider à écrire un nom, pas d'énumérer le roster à l'avance.
        La recherche porte sur tout le roster, jamais sur les 4 réponses du jour
        → elle ne révèle pas la bonne réponse.
        """
        payload = tokens.verify_token(request.args.get("t", ""), config.WEBAPP_SECRET)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 403
        today = today_str()
        if payload.get("d") != today:
            return jsonify({"error": "expired_token"}), 410

        mode = _payload_mode(payload)
        guild_id = payload["g"]
        user_id = payload["u"]

        # Hardcore = author + média, et seulement une fois "Jouer" cliqué en
        # Hardcore (sinon pas de suggestions du tout).
        if mode not in (database.MODE_AUTHOR, database.MODE_MEDIA):
            return jsonify({"results": [], "best": None})
        if database.get_daily_difficulty(guild_id, today, user_id, mode) != "hardcore":
            return jsonify({"results": [], "best": None})

        query = request.args.get("q", "")
        # Vide → liste complète (choix à la main) ; sinon top suggestions filtrées.
        limit = 200 if not query.strip() else 12
        results = database.search_members_ranked(guild_id, query, limit=limit)
        for r in results:
            r["user_id"] = str(r["user_id"])  # snowflake en chaîne

        # "best" = le 1er de la liste, mais seulement si on a tapé qqch (sur la
        # liste complète à saisie vide, rien n'est pré-sélectionné).
        best = (
            {"id": results[0]["user_id"], "name": results[0]["name"]}
            if (results and query.strip()) else None
        )
        return jsonify({"results": results, "best": best})

    @app.route("/daily/context")
    @app.route("/phrase/context")
    @app.route("/media/context")
    def daily_context():
        """Renvoie ±5 messages autour du défi (anti-spoil : faut avoir joué)."""
        token = request.args.get("t", "")
        payload = tokens.verify_token(token, config.WEBAPP_SECRET)
        if payload is None:
            return jsonify({"error": "invalid_token"}), 403
        today = today_str()
        if payload.get("d") != today:
            return jsonify({"error": "expired_token"}), 410

        mode = _payload_mode(payload)
        guild_id = payload["g"]
        user_id = payload["u"]

        attempt = database.get_daily_attempt(guild_id, today, user_id, mode=mode)
        if attempt is None:
            return jsonify({"error": "not_played_yet"}), 403

        ch = _load_challenge(guild_id, today, mode)
        if ch is None:
            return jsonify({"error": "no_daily"}), 404

        bot = current_app.config.get("BOT")
        before, after = fetch_message_context(
            bot, ch["channel_id"], ch["message_id"], n=5
        )
        return jsonify({
            "before": before,
            "after": after,
            "daily": {
                "author_name": ch["correct_name"],
                "content": ch["content_reveal"],
            },
        })

    return app


def run(host: Optional[str] = None, port: Optional[int] = None, bot=None) -> None:
    """Lance le serveur Flask. Utilisé par bot.py dans un thread démon.

    Le `bot` est passé pour que la route /daily/context puisse fetcher
    les messages voisins du daily directement via l'API Discord.
    """
    app = create_app(bot=bot)
    from waitress import serve

    serve(
        app,
        host=host or config.WEBAPP_HOST,
        port=port or config.WEBAPP_PORT,
        threads=config.WEBAPP_THREADS,
        clear_untrusted_proxy_headers=True,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    database.init_db()
    run()
