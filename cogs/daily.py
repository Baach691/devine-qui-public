"""Le défi quotidien : un même message pour tous, un essai par joueur par jour.

`/daily` renvoie un lien personnel vers le site web (Flask) où le joueur clique
sur ses 4 propositions. Le bot s'occupe juste de :
- créer le défi du jour si besoin,
- générer un token signé,
- afficher les stats persos en embed.
"""

import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database
import filters
import tokens

log = logging.getLogger(__name__)


# --- Helpers réutilisés (Discord + webapp) --------------------------------

def today_str() -> str:
    """Date du jour au format YYYY-MM-DD, dans le fuseau configuré."""
    try:
        tz = ZoneInfo(config.DAILY_TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date().isoformat()


_JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
_MOIS_FR = [
    "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def format_date_fr(date_str: str) -> str:
    """ISO 'YYYY-MM-DD' → date en toutes lettres, ex. 'Jeudi 19 Juin 2026'.

    Renvoie l'entrée inchangée si le format n'est pas reconnu.
    """
    try:
        d = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return date_str
    return f"{_JOURS_FR[d.weekday()]} {d.day} {_MOIS_FR[d.month - 1]} {d.year}"


def streak_emoji(streak: int) -> str:
    """Emoji adaptatif à la valeur de la streak (victoires).

    🧊 (0-1)  ·  🔥 (2-4)  ·  🚀 (5-9)  ·  💎 (10-24)  ·  ☢️ (25+)
    """
    if streak >= 25:
        return "☢️"
    if streak >= 10:
        return "💎"
    if streak >= 5:
        return "🚀"
    if streak >= 2:
        return "🔥"
    return "🧊"


def loss_streak_emoji(loss: int) -> str:
    """Emoji adaptatif à la série de défaites consécutives.

    🥶 (1)  ·  📉 (2-4)  ·  💀 (5-9)  ·  ⚰️ (10+)
    """
    if loss >= 10:
        return "⚰️"
    if loss >= 5:
        return "💀"
    if loss >= 2:
        return "📉"
    return "🥶"


def format_streak_segment(win_streak: int, loss_streak: int) -> str:
    """Segment d'affichage de la série en cours pour le classement.

    Série de victoires → `🔥 streak **+N**`.
    Série de défaites  → `📉 streak **-N**`.
    Aucune des deux    → `🧊 streak **0**`.
    """
    if win_streak > 0:
        return f"{streak_emoji(win_streak)} streak **+{win_streak}**"
    if loss_streak > 0:
        return f"{loss_streak_emoji(loss_streak)} streak **-{loss_streak}**"
    return "🧊 streak **0**"


def format_duration_ms(ms: Optional[float]) -> Optional[str]:
    """Format compact d'une durée en ms.

    < 60 s → précision milliseconde : '8.543s', '0.421s'
    < 1 h  → '1m05'
    sinon  → '1h12'
    """
    if ms is None or ms < 0:
        return None
    ms = int(ms)
    if ms < 60_000:
        return f"{ms / 1000:.3f}s"
    s = ms // 1000
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def global_name(user: discord.abc.User) -> str:
    """Pseudo Discord global du joueur (fallback sur le username)."""
    return getattr(user, "global_name", None) or user.name


def _parse_precompute_time() -> Optional[time]:
    """Parse DAILY_PRECOMPUTE_TIME ('HH:MM' ou vide/'off'). Renvoie None si désactivé."""
    raw = (getattr(config, "DAILY_PRECOMPUTE_TIME", "") or "").strip().lower()
    if not raw or raw in ("off", "false", "no", "none", "disabled"):
        return None
    try:
        hh, mm = raw.split(":")[:2] if ":" in raw else (raw, "0")
        hour, minute = int(hh), int(mm)
    except (ValueError, IndexError):
        log.warning(
            "DAILY_PRECOMPUTE_TIME invalide (%r) — fallback sur 08:00.", raw
        )
        hour, minute = 8, 0
    try:
        tz = ZoneInfo(config.DAILY_TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return time(hour=hour, minute=minute, tzinfo=tz)


def is_allowed(member) -> bool:
    """True si le membre a au moins un rôle whitelisté (ou si la whitelist est vide).

    `member` doit être un `discord.Member` (issu d'une interaction guild_only).
    """
    allowed = getattr(config, "ALLOWED_ROLE_IDS", []) or []
    if not allowed:
        return True
    roles = getattr(member, "roles", None) or []
    return any(r.id in allowed for r in roles)


# --- Cog ------------------------------------------------------------------

def _build_options(
    guild_id: int, author_id: int, author_name: str, channel_id=None
) -> List[Tuple[int, str]]:
    distractors = database.get_distinct_authors(
        guild_id, exclude_id=author_id, limit=3, channel_id=channel_id
    )
    options = distractors + [(author_id, author_name)]
    random.shuffle(options)
    return options


def _user_avatar_url(user: discord.abc.User) -> str:
    """URL de l'avatar du joueur (vide si rien)."""
    avatar = getattr(user, "display_avatar", None)
    return str(avatar.url) if avatar else ""


def _format_results_list(results, mode=database.MODE_AUTHOR) -> str:
    """Liste à puces avec statut ✅/❌, difficulté (mode auteur seulement) et temps.

    Temps affiché avec 3 décimales. L'emoji de difficulté n'apparaît qu'en mode
    auteur (seul mode avec Hardcore) — inutile pour phrase et média.
    """
    show_diff = mode == database.MODE_AUTHOR
    lines = []
    for r in results:
        emoji = "✅" if r["correct"] else "❌"
        line = f"{emoji} **{r['user_name']}**"
        if show_diff:
            line += " · " + ("💀" if r.get("difficulty") == "hardcore" else "😀")
        ms = r.get("time_taken_ms")
        if ms is not None:
            line += f" · {ms / 1000:.3f}s"
        broken = r.get("last_broken_streak", 0) or 0
        if broken >= 2 and r.get("last_broken_date") == r.get("date"):
            line += f" · 💔{broken}"
        if sum(len(x) + 1 for x in lines) + len(line) > 3950:
            break
        lines.append(line)
    return "\n".join(lines)


# --- Affichage multi-mode (boutons de bascule sur /classement, etc.) ------

MODE_LABEL = {
    database.MODE_AUTHOR: "🕵️ Qui a écrit ça ?",
    database.MODE_PHRASE: "✍️ Devine la phrase",
    database.MODE_MEDIA: "🖼️ Devine le média",
}

# Emoji court par mode (récap "Général" de /daily-resultats).
MODE_EMOJI = {
    database.MODE_AUTHOR: "🕵️",
    database.MODE_PHRASE: "✍️",
    database.MODE_MEDIA: "🖼️",
}

# Pseudo-mode pour l'onglet récap de /daily-resultats (PAS un vrai mode BDD :
# il agrège les 3 modes, jamais passé à get_daily_results).
MODE_GENERAL = "general"
MODE_LABEL[MODE_GENERAL] = "📊 Général"

# Modes affichés dans /classement (les 3 modes).
CLASSEMENT_MODES = (database.MODE_AUTHOR, database.MODE_PHRASE, database.MODE_MEDIA)
# Modes affichés dans /daily-resultats, /winner, /loser (média inclus).
RESULTS_MODES = (database.MODE_AUTHOR, database.MODE_PHRASE, database.MODE_MEDIA)


def _streak_badge(r) -> str:
    win, loss = r["current_streak"], r.get("current_loss_streak", 0) or 0
    if win > 0:
        return f"{streak_emoji(win)}+{win}"
    if loss > 0:
        return f"{loss_streak_emoji(loss)}-{loss}"
    return "🧊0"


def build_leaderboard_embed(guild_id: int, mode: str) -> discord.Embed:
    """Classement ALLÉGÉ (/classement) : victoires, ratio, streak actuelle."""
    rows = database.get_leaderboard(guild_id, mode=mode)
    label = MODE_LABEL[mode]
    if not rows:
        return discord.Embed(
            title=f"🏆 Classement — {label}",
            description="Aucun score pour l'instant sur ce mode. Lance le défi ! 🌞",
            color=discord.Color.gold(),
        )
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        rank = medals[i] if i < 3 else f"`{i + 1:>2}.`"
        pct = round(100 * r["correct"] / r["total"]) if r["total"] else 0
        line = (
            f"{rank} **{r['name']}** · {r['correct']} victoires · "
            f"{pct}% · {_streak_badge(r)}"
        )
        if sum(len(x) + 1 for x in lines) + len(line) > 3950:
            break
        lines.append(line)
    return discord.Embed(
        title=f"🏆 Classement — {label}  ·  {len(lines)} joueurs",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )


def build_leaderboard_embed_full(guild_id: int, mode: str) -> discord.Embed:
    """Classement COMPLET (/classement-complet) : toutes les stats."""
    rows = database.get_leaderboard(guild_id, mode=mode)
    label = MODE_LABEL[mode]
    if not rows:
        return discord.Embed(
            title=f"🏆 Classement complet — {label}",
            description="Aucun score pour l'instant sur ce mode. Lance le défi ! 🌞",
            color=discord.Color.gold(),
        )
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        rank = medals[i] if i < 3 else f"`{i + 1:>2}.`"
        pct = round(100 * r["correct"] / r["total"]) if r["total"] else 0
        avg_ms = r.get("avg_time_ms")
        avg_str = f" · {avg_ms / 1000:.1f}s" if avg_ms is not None else ""
        hc = r.get("hardcore_count", 0) or 0
        nm = r.get("normal_count", max(0, r["total"] - hc))
        line = (
            f"{rank} **{r['name']}** · {r.get('points', r['correct'])}pts · "
            f"{r['correct']}/{r['total']} ({pct}%) · {_streak_badge(r)} · "
            f"💀{hc}😀{nm}{avg_str} · 🏆{r['best_streak']}"
        )
        if sum(len(x) + 1 for x in lines) + len(line) > 3950:
            break
        lines.append(line)
    return discord.Embed(
        title=f"🏆 Classement complet — {label}  ·  {len(lines)} joueurs",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )


def build_results_embed(guild_id: int, mode: str) -> discord.Embed:
    """Embed des tentatives du jour pour un mode (toujours un embed)."""
    date_str = today_str()
    label = MODE_LABEL[mode]
    results = database.get_daily_results(guild_id, date_str, mode=mode)
    if not results:
        return discord.Embed(
            title=f"🌞 Daily du {format_date_fr(date_str)} — {label}",
            description="Personne n'a encore tenté ce mode aujourd'hui. Sois le premier 🌞",
            color=discord.Color.gold(),
        )
    correct_count = sum(1 for r in results if r["correct"])
    embed = discord.Embed(
        title=f"🌞 Daily du {format_date_fr(date_str)} — {label}",
        description=_format_results_list(results, mode),
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=f"{correct_count} / {len(results)} ont trouvé — la réponse n'est révélée "
             f"qu'après avoir joué (pas de spoil 🤫)."
    )
    return embed


def build_filtered_results_embed(
    guild_id: int, mode: str, *, wanted_correct: bool, title: str, color: discord.Color
) -> discord.Embed:
    """Embed des gagnants/perdants du jour pour un mode (toujours un embed)."""
    date_str = today_str()
    label = MODE_LABEL[mode]
    results = [
        r for r in database.get_daily_results(guild_id, date_str, mode=mode)
        if r["correct"] is wanted_correct
    ]
    if not results:
        verb = "gagnant" if wanted_correct else "perdant"
        return discord.Embed(
            title=f"{title} — {label}",
            description=f"Aucun {verb} sur ce mode aujourd'hui.",
            color=color,
        )
    return discord.Embed(
        title=f"{title} · {format_date_fr(date_str)} — {label}",
        description=_format_results_list(results, mode),
        color=color,
    )


def build_general_results_embed(guild_id: int) -> discord.Embed:
    """Récap du jour TOUS MODES : qui a réussi quoi (✅/❌ par mode + score X/N).

    N = nombre de modes auxquels le joueur a participé (donc /3 s'il a tout fait,
    /1 ou /2 sinon). Classé du meilleur au moins bon."""
    date_str = today_str()
    modes = (database.MODE_AUTHOR, database.MODE_PHRASE, database.MODE_MEDIA)
    title = f"🌞 Daily du {format_date_fr(date_str)} — {MODE_LABEL[MODE_GENERAL]}"

    # Agrégat par joueur : {user_id: {"name": ..., "modes": {mode: bool}}}
    players: dict = {}
    for m in modes:
        for r in database.get_daily_results(guild_id, date_str, mode=m):
            p = players.setdefault(r["user_id"], {"name": r["user_name"], "modes": {}})
            p["name"] = r["user_name"]
            p["modes"][m] = bool(r["correct"])

    if not players:
        return discord.Embed(
            title=title,
            description="Personne n'a encore participé aujourd'hui. Sois le premier 🌞",
            color=discord.Color.gold(),
        )

    def score(p):
        ok = sum(1 for v in p["modes"].values() if v)
        tot = len(p["modes"])
        return ok, tot

    def sort_key(item):
        ok, tot = score(item[1])
        return (-ok, -(ok / tot), item[1]["name"].lower())

    lines = []
    for _uid, p in sorted(players.items(), key=sort_key):
        ok, tot = score(p)
        per = " ".join(
            f"{MODE_EMOJI[m]}{'✅' if p['modes'][m] else '❌'}"
            for m in modes if m in p["modes"]
        )
        line = f"`{ok}/{tot}` **{p['name']}** {per}"
        if sum(len(x) + 1 for x in lines) + len(line) > 3950:
            break
        lines.append(line)

    embed = discord.Embed(
        title=title, description="\n".join(lines), color=discord.Color.gold()
    )
    embed.set_footer(text="🕵️ Qui a écrit ça ? · ✍️ Devine la phrase · 🖼️ Devine le média")
    return embed


class _ModeButton(discord.ui.Button):
    def __init__(self, mode: str, *, active: bool):
        super().__init__(
            label=MODE_LABEL[mode],
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
        )
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        view: "ModeSwitchView" = self.view
        if not is_allowed(interaction.user):
            await _deny_unauthorized(interaction)
            return
        embed = view.render(self.mode)
        for child in view.children:
            child.style = (
                discord.ButtonStyle.primary
                if getattr(child, "mode", None) == self.mode
                else discord.ButtonStyle.secondary
            )
        await interaction.response.edit_message(embed=embed, view=view)


class ModeSwitchView(discord.ui.View):
    """Vue à 2 boutons pour basculer l'affichage entre les modes de jeu."""

    def __init__(self, render, current_mode: str, *, modes=None, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.render = render  # callable(mode) -> discord.Embed
        for mode in (modes or (database.MODE_AUTHOR, database.MODE_PHRASE)):
            self.add_item(_ModeButton(mode, active=(mode == current_mode)))


def _build_daily_link(
    guild_id: int, user_id: int, date_str: str, user_name: str, user_avatar: str,
    mode: str = database.MODE_AUTHOR,
    activity: bool = False,
) -> str:
    """Génère le lien signé vers le site pour ce joueur, ce jour et ce mode."""
    payload = {
        "g": guild_id,
        "u": user_id,
        "d": date_str,
        "n": user_name,
        "a": user_avatar,
    }
    # On n'ajoute le mode au payload que pour le mode phrase, pour rester
    # rétro-compatible avec les liens 'author' déjà émis (sans clé 'm').
    if mode != database.MODE_AUTHOR:
        payload["m"] = mode
    if activity:
        payload["x"] = "activity"
    token = tokens.make_token(payload, config.WEBAPP_SECRET)
    if activity:
        return f"/.proxy/daily?t={token}"
    base = config.WEBAPP_BASE_URL.rstrip("/")
    return f"{base}/daily?t={token}"


async def _deny_unauthorized(interaction: discord.Interaction) -> None:
    """Réponse standardisée pour un user hors whitelist."""
    await interaction.response.send_message(
        "🔒 Tu n'es pas dans la liste des joueurs autorisés sur ce bot. "
        "Demande à l'admin pour t'ajouter.",
        ephemeral=True,
    )


# --- Pick en live depuis l'API Discord (plus besoin de /backfill) ---------

def _pickable_channels(guild: discord.Guild) -> List[discord.TextChannel]:
    """Salons d'où on peut tirer un daily : ALLOWED_CHANNEL_IDS si défini,
    sinon n'importe quel salon texte où le bot a accès à l'historique."""
    allowed = getattr(config, "ALLOWED_CHANNEL_IDS", []) or []
    candidates = guild.text_channels
    if allowed:
        candidates = [c for c in candidates if c.id in allowed]
    me = guild.me
    return [c for c in candidates if c.permissions_for(me).read_message_history]


async def _pick_daily_live(
    guild_id: int, channels: List[discord.TextChannel]
) -> Optional[Tuple[discord.Message, List[Tuple[int, str]]]]:
    """Tire un message éligible au hasard depuis l'API Discord live.

    Algo :
      1. Choisit une date au hasard entre OLDEST_MESSAGE_DATE et aujourd'hui.
      2. Fetch jusqu'à 200 messages dans une fenêtre autour, applique filtres,
         vire blacklist + opt-out + tirés récemment.
      3. Si aucun éligible → élargit la fenêtre (7 → 30 → 90 jours).
      4. Si toujours rien → re-tente avec une autre date (3 essais max).
      5. Distracteurs : auteurs vus dans la fenêtre, complétés depuis la BDD
         (`get_distinct_authors`) si on en a moins de 3.
      6. Minimum 2 options (1 bonne + 1 distracteur) pour avoir un QCM jouable.

    Renvoie (message, [(author_id, name), ...]) ou None si vraiment rien trouvé.
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
        guild_id, limit=config.RECENT_PICKS_EXCLUDE
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
            eligible: List[discord.Message] = []
            authors_seen: dict = {}
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
                    eligible.append(msg)
                    authors_seen[msg.author.id] = msg.author
            except discord.DiscordException as e:
                log.warning("Erreur fetch live (#%s, %s): %s", channel.name, target, e)
                continue

            log.info(
                "Pick #%d ±%dj autour de %s dans #%s : scannés=%d, éligibles=%d, auteurs=%d",
                attempt_idx + 1, window, target, channel.name,
                scanned, len(eligible), len(authors_seen),
            )

            if not eligible:
                continue

            # Le daily : un message éligible au hasard dans la fenêtre.
            daily_msg = random.choice(eligible)
            daily_author_id = daily_msg.author.id
            for aid, user in authors_seen.items():
                database.upsert_user(guild_id, aid, global_name(user), _user_avatar_url(user))

            # Distracteurs : 1) auteurs vus dans la fenêtre.
            distractor_pool: dict = {}
            for aid, user in authors_seen.items():
                if aid != daily_author_id:
                    distractor_pool[aid] = global_name(user)

            # 2) On complète depuis la BDD si moins de 3 distracteurs disponibles.
            if len(distractor_pool) < 3:
                db_authors = database.get_distinct_authors(
                    guild_id, exclude_id=daily_author_id, limit=10
                )
                for aid, name in db_authors:
                    if len(distractor_pool) >= 3:
                        break
                    if aid not in distractor_pool and aid != daily_author_id:
                        distractor_pool[aid] = name

            if not distractor_pool:
                # Aucun autre auteur en vue → QCM impossible, on tente une autre date.
                log.info("Aucun distracteur (window+BDD) — nouvelle tentative.")
                continue

            # Échantillonnage : jusqu'à 3 distracteurs.
            distractor_ids = random.sample(
                list(distractor_pool.keys()), k=min(3, len(distractor_pool))
            )
            options = [(aid, distractor_pool[aid]) for aid in distractor_ids]
            options.append((daily_author_id, global_name(daily_msg.author)))

            random.shuffle(options)
            return daily_msg, options

    return None


class Daily(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._initial_precompute_done = False

        # Programmation du pré-calcul quotidien (si activé dans la config).
        scheduled = _parse_precompute_time()
        if scheduled is not None:
            self.precompute_loop.change_interval(time=scheduled)
            self.precompute_loop.start()
            log.info(
                "Pré-calcul daily programmé à %s (%s).",
                scheduled.strftime("%H:%M"), scheduled.tzinfo,
            )
        else:
            log.info("Pré-calcul daily désactivé (DAILY_PRECOMPUTE_TIME vide).")

    def cog_unload(self):
        if self.precompute_loop.is_running():
            self.precompute_loop.cancel()

    # --- Pré-calcul automatique ------------------------------------------

    @tasks.loop(time=time(hour=0, minute=0, tzinfo=timezone.utc))
    async def precompute_loop(self):
        """Au reset : tire le défi du jour pour chaque guild, puis annonce."""
        await self._precompute_for_all_guilds(label="cron")

    @precompute_loop.before_loop
    async def _before_precompute_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        """Rattrapage au boot : tire le défi du jour s'il manque, puis annonce
        s'il n'a pas encore été annoncé (utile si le bot était éteint à minuit)."""
        if self._initial_precompute_done:
            return
        self._initial_precompute_done = True
        if _parse_precompute_time() is None:
            return
        await self._precompute_for_all_guilds(label="boot")

    async def _precompute_for_all_guilds(self, *, label: str) -> None:
        from cogs.media import ensure_media_daily_for_guild
        from cogs.phrase import ensure_phrase_daily_for_guild

        date_str = today_str()
        for guild in list(self.bot.guilds):
            # On prépare les TROIS modes ici (scheduler unique), puis une seule
            # annonce combinée — pour ne pas envoyer 3 pings à minuit.
            await self._precompute_for_guild(guild, date_str, label=label)
            try:
                await ensure_phrase_daily_for_guild(guild, date_str, label=label)
            except Exception:
                log.exception("Pré-calcul phrase (%s) : échec pour %s.", label, guild.name)
            try:
                await ensure_media_daily_for_guild(guild, date_str, label=label)
            except Exception:
                log.exception("Pré-calcul média (%s) : échec pour %s.", label, guild.name)
            await self._announce_new_daily(guild, date_str)

    async def _precompute_for_guild(
        self, guild: discord.Guild, date_str: str, *, label: str
    ) -> None:
        if database.get_daily(guild.id, date_str) is not None:
            log.debug(
                "Pré-calcul (%s) : daily déjà présent pour %s — skip.",
                label, guild.name,
            )
            return
        channels = _pickable_channels(guild)
        if not channels:
            log.warning(
                "Pré-calcul (%s) : aucun salon utilisable pour %s.",
                label, guild.name,
            )
            return
        try:
            picked = await _pick_daily_live(guild.id, channels)
        except Exception:
            log.exception(
                "Pré-calcul (%s) : exception pour %s.", label, guild.name
            )
            return
        if picked is None:
            log.warning(
                "Pré-calcul (%s) : aucun message éligible pour %s. Causes possibles : "
                "salon trop petit, filtres trop stricts (MIN_CHARS=%d MIN_WORDS=%d), "
                "ou pas encore d'auteurs distincts dans la BDD. "
                "Astuce : lance /backfill une fois pour seed la BDD. "
                "Le premier /daily retentera.",
                label, guild.name, config.MIN_CHARS, config.MIN_WORDS,
            )
            return
        live_msg, options = picked
        created = database.create_daily_if_absent(
            guild.id, date_str, live_msg.id, live_msg.channel.id,
            live_msg.author.id, global_name(live_msg.author),
            live_msg.content, options,
        )
        if created:
            database.record_pick(guild.id, live_msg.id)
            log.info(
                "Pré-calcul (%s) ✅ pour %s : msg #%s par %s dans #%s.",
                label, guild.name, live_msg.id,
                global_name(live_msg.author), live_msg.channel.name,
            )
        else:
            log.info(
                "Pré-calcul (%s) : course perdue pour %s — un autre process a déjà créé le daily.",
                label, guild.name,
            )

    def _resolve_announce_channel(self, guild: discord.Guild, date_str: str):
        """Détermine le salon d'annonce, par ordre de priorité :

        1. ANNOUNCE_CHANNEL_ID si défini dans le .env.
        2. Sinon, le salon whitelisté (ALLOWED_CHANNEL_IDS) — typiquement #général.
        3. Sinon, le salon d'où provient le message tiré aujourd'hui.

        Renvoie un salon textuel postable par le bot, ou None.
        """
        candidate_ids = []

        explicit = getattr(config, "ANNOUNCE_CHANNEL_ID", 0) or 0
        if explicit:
            candidate_ids.append(explicit)

        candidate_ids.extend(getattr(config, "ALLOWED_CHANNEL_IDS", []) or [])

        daily = database.get_daily(guild.id, date_str)
        if daily is not None:
            candidate_ids.append(daily["channel_id"])

        me = guild.me
        for cid in candidate_ids:
            channel = guild.get_channel(cid)
            if channel is None:
                continue
            if not isinstance(channel, discord.TextChannel):
                continue
            if channel.permissions_for(me).send_messages:
                return channel
        return None

    async def _announce_new_daily(self, guild: discord.Guild, date_str: str) -> None:
        """Annonce le daily du jour (+ ping les joueurs d'hier), au plus une fois/jour.

        Idempotente grâce à la table daily_announced : peu importe combien de fois
        cette méthode est appelée (cron, boot, restarts), l'annonce ne part qu'une
        seule fois par jour et par serveur.
        """
        # Déjà annoncé aujourd'hui ? on ne refait rien.
        if database.is_daily_announced(guild.id, date_str):
            return

        # Rien à annoncer s'il n'y a pas de daily pour aujourd'hui.
        if database.get_daily(guild.id, date_str) is None:
            return

        channel = self._resolve_announce_channel(guild, date_str)
        if channel is None:
            # On ne marque PAS comme annoncé : un prochain passage (re-perm,
            # config corrigée) pourra réessayer sans qu'on ait spammé.
            log.warning("Aucun salon d'annonce postable pour %s — annonce reportée.", guild.name)
            return

        # Modes disponibles aujourd'hui (un seul message pour les trois).
        available = []
        if database.get_daily(guild.id, date_str) is not None:
            available.append("🌞 Qui a écrit ça ?")
        if database.get_phrase_daily(guild.id, date_str) is not None:
            available.append("✍️ Devine la phrase")
        if database.get_daily(guild.id, date_str, mode=database.MODE_MEDIA) is not None:
            available.append("🖼️ Devine le média")
        modes_txt = " · ".join(available)

        # Joueurs d'hier = union sur les trois modes (chaque membre pingé 1 fois).
        yesterday = (date.fromisoformat(date_str) - timedelta(days=1)).isoformat()
        played_yesterday = {}
        for m in (database.MODE_AUTHOR, database.MODE_PHRASE, database.MODE_MEDIA):
            for r in database.get_daily_results(guild.id, yesterday, mode=m):
                played_yesterday.setdefault(r["user_id"], None)

        if played_yesterday:
            mentions = " ".join(f"<@{uid}>" for uid in played_yesterday)
            content = (
                f"🎮 **Daily du {format_date_fr(date_str)} disponible !**\n"
                f"Modes du jour : {modes_txt}\n\n"
                f"À vous {mentions} — tapez `/daily` !"
            )
        else:
            content = (
                f"🎮 **Daily du {format_date_fr(date_str)} disponible !**\n"
                f"Modes du jour : {modes_txt}\n"
                f"Lancez `/daily` pour jouer 🎮"
            )

        try:
            await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except discord.DiscordException:
            log.exception("Échec de l'annonce dans #%s — sera retentée.", channel.name)
            return

        # Marque seulement après un envoi réussi (sinon on retentera plus tard).
        database.mark_daily_announced(guild.id, date_str)
        log.info("Annonce combinée envoyée dans #%s (%s joueur(s) pingé(s)).",
                 channel.name, len(played_yesterday))

    @app_commands.command(
        name="daily",
        description="Lance l'Activity pour jouer aux trois défis du jour !",
    )
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction):
        if not is_allowed(interaction.user):
            await _deny_unauthorized(interaction)
            return

        guild_id = interaction.guild_id
        date_str = today_str()
        user_id = interaction.user.id
        user_name = global_name(interaction.user)
        user_avatar = _user_avatar_url(interaction.user)

        # On enregistre le profil pour que l'avatar apparaisse dans la liste des joueurs.
        database.upsert_user(guild_id, user_id, user_name, user_avatar)

        # Crée les défis du jour s'ils n'existent pas encore.
        # Le pick live peut prendre 2-5s (appels API Discord) → on defer dans ce cas.
        d = database.get_daily(guild_id, date_str)
        phrase_daily = database.get_phrase_daily(guild_id, date_str)
        media_daily = database.get_daily(guild_id, date_str, mode=database.MODE_MEDIA)

        # Quand le pré-calcul quotidien a bien préparé les trois modes, /daily
        # ouvre directement l'Activity Discord. Le reste de la commande demeure
        # un fallback : il prépare les défis manquants ou fournit le lien web si
        # Discord refuse exceptionnellement le lancement.
        if d is not None and phrase_daily is not None and media_daily is not None:
            try:
                await interaction.response.launch_activity()
                return
            except discord.HTTPException:
                log.exception(
                    "Impossible de lancer l'Activity via /daily "
                    "(guild=%s user=%s). Fallback vers le lien web.",
                    guild_id,
                    user_id,
                )

        # On defer dès qu'UN des trois modes doit être tiré (pick live = 2-5 s),
        # sinon l'interaction Discord (3 s) expire avant la réponse.
        if d is None or phrase_daily is None or media_daily is None:
            await interaction.response.defer(ephemeral=True, thinking=True)

        if d is None:
            channels = _pickable_channels(interaction.guild)
            if not channels:
                await interaction.followup.send(
                    "Aucun salon accessible pour le tirage. Vérifie "
                    "`ALLOWED_CHANNEL_IDS` et que le bot a bien Read Message History. 📭",
                    ephemeral=True,
                )
                return

            picked = await _pick_daily_live(guild_id, channels)
            if picked is not None:
                live_msg, options = picked
                msg_id = live_msg.id
                ch_id = live_msg.channel.id
                author_id = live_msg.author.id
                author_name = global_name(live_msg.author)
                content = live_msg.content
            else:
                # Fallback : on tente la BDD locale (utile si on_message a déjà
                # accumulé des messages ou si /backfill a été lancé un jour).
                row = database.get_random_message(guild_id)
                if row is None:
                    await interaction.followup.send(
                        "Aucun message éligible trouvé en live ni en base 😕\n"
                        "• Vérifie que le bot a accès à un salon avec assez d'historique.\n"
                        "• Étends éventuellement `OLDEST_MESSAGE_DATE` dans `.env`.",
                        ephemeral=True,
                    )
                    return
                msg_id, ch_id, author_id, author_name, content, _ = row
                options = _build_options(guild_id, author_id, author_name)
                if len(options) < 2:
                    await interaction.followup.send(
                        "Pas assez d'auteurs différents pour proposer un QCM (fallback BDD). 🙃",
                        ephemeral=True,
                    )
                    return

            created = database.create_daily_if_absent(
                guild_id, date_str, msg_id, ch_id,
                author_id, author_name, content, options,
            )
            if created:
                database.record_pick(guild_id, msg_id)
            # Qu'on l'ait créé ou perdu la course, on relit la version canonique.
            d = database.get_daily(guild_id, date_str)

        phrase_error = None
        if database.get_phrase_daily(guild_id, date_str) is None:
            from cogs.phrase import ensure_phrase_daily_for_guild

            _, phrase_error = await ensure_phrase_daily_for_guild(
                interaction.guild, date_str, label="/daily"
            )

        media_error = None
        if database.get_daily(guild_id, date_str, mode=database.MODE_MEDIA) is None:
            from cogs.media import ensure_media_daily_for_guild

            _, media_error = await ensure_media_daily_for_guild(
                interaction.guild, date_str, label="/daily"
            )

        # Stats du joueur + nb de tentatives du jour, pour les deux modes.
        attempt = database.get_daily_attempt(
            guild_id, date_str, user_id, mode=database.MODE_AUTHOR
        )
        phrase_attempt = database.get_daily_attempt(
            guild_id, date_str, user_id, mode=database.MODE_PHRASE
        )
        current_streak, best_streak = database.get_streak(
            guild_id, user_id, today_str=date_str
        )
        phrase_streak, phrase_best = database.get_streak(
            guild_id, user_id, today_str=date_str, mode=database.MODE_PHRASE
        )
        media_attempt = database.get_daily_attempt(
            guild_id, date_str, user_id, mode=database.MODE_MEDIA
        )
        media_streak, media_best = database.get_streak(
            guild_id, user_id, today_str=date_str, mode=database.MODE_MEDIA
        )
        results = database.get_daily_results(guild_id, date_str, mode=database.MODE_AUTHOR)
        phrase_results = database.get_daily_results(
            guild_id, date_str, mode=database.MODE_PHRASE
        )
        media_results = database.get_daily_results(
            guild_id, date_str, mode=database.MODE_MEDIA
        )
        phrase_available = database.get_phrase_daily(guild_id, date_str) is not None
        media_available = database.get_daily(
            guild_id, date_str, mode=database.MODE_MEDIA
        ) is not None

        url = _build_daily_link(guild_id, user_id, date_str, user_name, user_avatar)

        def _status(a):
            if a is None:
                return "À jouer"
            return "✅ Réussi" if a["correct"] else "❌ Raté"

        playable_modes = 1 + (1 if phrase_available else 0) + (1 if media_available else 0)
        played_modes = 1 if attempt is not None else 0
        if phrase_available and phrase_attempt is not None:
            played_modes += 1
        if media_available and media_attempt is not None:
            played_modes += 1
        if played_modes >= playable_modes:
            embed = discord.Embed(
                title=f"🌞 Daily du {format_date_fr(date_str)}",
                description="Tu as déjà joué aux modes disponibles aujourd'hui. Clique pour revoir le site.",
                color=discord.Color.green(),
            )
            button_label = "Voir le site 🌐"
        elif played_modes:
            embed = discord.Embed(
                title=f"🌞 Daily du {format_date_fr(date_str)}",
                description="Il te reste un mode à jouer sur le site.",
                color=discord.Color.blurple(),
            )
            button_label = "Continuer 🎮"
        else:
            embed = discord.Embed(
                title=f"🌞 Daily du {format_date_fr(date_str)}",
                description=(
                    "Clique sur **Ouvrir le site 🎮** : tu y trouveras les onglets "
                    "**Qui a écrit ça ?**, **Devine la phrase** et **Devine le média**."
                ),
                color=discord.Color.gold(),
            )
            button_label = "Ouvrir le site 🎮"

        embed.add_field(
            name="🌞 Qui a écrit ça ?",
            value=(
                f"{_status(attempt)}\n"
                f"{streak_emoji(current_streak)} streak **{current_streak}** · "
                f"record **{best_streak}**"
            ),
            inline=True,
        )
        if phrase_available:
            phrase_value = (
                f"{_status(phrase_attempt)}\n"
                f"{streak_emoji(phrase_streak)} streak **{phrase_streak}** · "
                f"record **{phrase_best}**"
            )
        else:
            phrase_value = phrase_error or "Indisponible pour l'instant."
        embed.add_field(
            name="✍️ Devine la phrase",
            value=phrase_value,
            inline=True,
        )
        if media_available:
            media_value = (
                f"{_status(media_attempt)}\n"
                f"{streak_emoji(media_streak)} streak **{media_streak}** · "
                f"record **{media_best}**"
            )
        else:
            media_value = media_error or "Indisponible pour l'instant."
        embed.add_field(
            name="🖼️ Devine le média",
            value=media_value,
            inline=True,
        )
        embed.add_field(
            name="👥 Aujourd'hui",
            value=(
                f"Qui a écrit ça ? : **{len(results)}** · "
                f"Phrase : **{len(phrase_results)}** · "
                f"Média : **{len(media_results)}** joueur(s)"
            ),
            inline=False,
        )
        embed.set_footer(text="Un seul essai par mode et par jour. Même défi pour tout le serveur.")

        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            style=discord.ButtonStyle.link, label=button_label, url=url,
        ))

        # Si on a déjà defer (pick live), utiliser followup. Sinon réponse directe.
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="mes-stats", description="Voir tes statistiques personnelles au daily."
    )
    @app_commands.guild_only()
    async def mes_stats(self, interaction: discord.Interaction):
        if not is_allowed(interaction.user):
            await _deny_unauthorized(interaction)
            return

        guild_id = interaction.guild_id
        user_id = interaction.user.id
        today = today_str()
        name = global_name(interaction.user)

        database.upsert_user(guild_id, user_id, name, _user_avatar_url(interaction.user))

        embed = discord.Embed(
            title=f"📊 Stats — {name}",
            color=discord.Color.blurple(),
        )
        # Une ligne par mode : Qui a écrit ça ?, Devine la phrase, Devine le média.
        for mode, label in (
            (database.MODE_AUTHOR, "🌞 Qui a écrit ça ?"),
            (database.MODE_PHRASE, "✍️ Devine la phrase"),
            (database.MODE_MEDIA, "🖼️ Devine le média"),
        ):
            cur, bst = database.get_streak(guild_id, user_id, today_str=today, mode=mode)
            corr, tot = database.get_user_score(guild_id, user_id, mode=mode)
            pct = round(100 * corr / tot) if tot else 0
            embed.add_field(
                name=label,
                value=(
                    f"{corr}/{tot} ({pct} %) · {streak_emoji(cur)} streak **{cur}** · "
                    f"🏆 record **{bst}**"
                ),
                inline=False,
            )
        embed.set_footer(text="Joue chaque jour avec /daily pour faire grimper tes streaks 🌞")
        avatar = interaction.user.display_avatar
        if avatar:
            embed.set_thumbnail(url=avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="classement",
        description="Classement allégé : victoires, ratio, streak (Qui a écrit ça ? / la phrase / le média).",
    )
    @app_commands.guild_only()
    async def classement(self, interaction: discord.Interaction):
        if not is_allowed(interaction.user):
            await _deny_unauthorized(interaction)
            return
        gid = interaction.guild_id
        render = lambda mode: build_leaderboard_embed(gid, mode)
        view = ModeSwitchView(render, database.MODE_AUTHOR, modes=CLASSEMENT_MODES)
        await interaction.response.send_message(
            embed=render(database.MODE_AUTHOR), view=view
        )

    @app_commands.command(
        name="classement-complet",
        description="Classement complet : toutes les stats (Qui a écrit ça ? / la phrase / le média).",
    )
    @app_commands.guild_only()
    async def classement_complet(self, interaction: discord.Interaction):
        if not is_allowed(interaction.user):
            await _deny_unauthorized(interaction)
            return
        gid = interaction.guild_id
        render = lambda mode: build_leaderboard_embed_full(gid, mode)
        view = ModeSwitchView(render, database.MODE_AUTHOR, modes=CLASSEMENT_MODES)
        await interaction.response.send_message(
            embed=render(database.MODE_AUTHOR), view=view
        )

    @app_commands.command(
        name="daily-resultats",
        description="Tentatives du jour (4 onglets : Qui a écrit ça ? / la phrase / le média / Général).",
    )
    @app_commands.guild_only()
    async def daily_results(self, interaction: discord.Interaction):
        if not is_allowed(interaction.user):
            await _deny_unauthorized(interaction)
            return
        gid = interaction.guild_id
        render = lambda mode: (
            build_general_results_embed(gid)
            if mode == MODE_GENERAL else build_results_embed(gid, mode)
        )
        view = ModeSwitchView(
            render, database.MODE_AUTHOR, modes=RESULTS_MODES + (MODE_GENERAL,)
        )
        await interaction.response.send_message(
            embed=render(database.MODE_AUTHOR), view=view
        )

    @app_commands.command(
        name="winner",
        description="Gagnants du jour (2 boutons : Qui a écrit ça ? / Devine la phrase).",
    )
    @app_commands.guild_only()
    async def winner(self, interaction: discord.Interaction):
        if not is_allowed(interaction.user):
            await _deny_unauthorized(interaction)
            return
        gid = interaction.guild_id
        render = lambda mode: build_filtered_results_embed(
            gid, mode, wanted_correct=True,
            title="✅ Gagnants du daily", color=discord.Color.green(),
        )
        view = ModeSwitchView(render, database.MODE_AUTHOR, modes=RESULTS_MODES)
        await interaction.response.send_message(
            embed=render(database.MODE_AUTHOR), view=view
        )

    @app_commands.command(
        name="loser",
        description="Perdants du jour (2 boutons : Qui a écrit ça ? / Devine la phrase).",
    )
    @app_commands.guild_only()
    async def loser(self, interaction: discord.Interaction):
        if not is_allowed(interaction.user):
            await _deny_unauthorized(interaction)
            return
        gid = interaction.guild_id
        render = lambda mode: build_filtered_results_embed(
            gid, mode, wanted_correct=False,
            title="❌ Perdants du daily", color=discord.Color.red(),
        )
        view = ModeSwitchView(render, database.MODE_AUTHOR, modes=RESULTS_MODES)
        await interaction.response.send_message(
            embed=render(database.MODE_AUTHOR), view=view
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Daily(bot))
