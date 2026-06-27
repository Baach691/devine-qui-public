"""Alimentation de la base : écoute en direct + commande /backfill."""

from datetime import date, datetime, timezone
from typing import List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
import filters


def _avatar_url(user: discord.abc.User) -> str:
    avatar = getattr(user, "display_avatar", None)
    return str(avatar.url) if avatar else ""


def _year_buckets(total_limit: int) -> List[Tuple[Optional[int], int, Optional[datetime], Optional[datetime]]]:
    """Découpe `total_limit` entre toutes les années entre OLDEST_MESSAGE_DATE
    et aujourd'hui, pour répartir le scan équitablement par année.

    Renvoie une liste de (year, bucket_size, after_dt, before_dt).
    Si la plage est vide ou invalide, renvoie un seul bucket "fallback" sans
    contrainte de date — comportement historique.
    """
    try:
        oldest = date.fromisoformat(config.OLDEST_MESSAGE_DATE)
    except Exception:
        oldest = date(2023, 10, 1)
    today = date.today()
    if today < oldest:
        return [(None, total_limit, None, None)]

    years = list(range(oldest.year, today.year + 1))
    if not years:
        return [(None, total_limit, None, None)]
    per_bucket = max(1, total_limit // len(years))

    out = []
    for y in years:
        year_start = max(oldest, date(y, 1, 1))
        # Borne haute exclusive : minuit du 1er janvier de l'année suivante
        # (ou demain si on est sur l'année en cours).
        from datetime import timedelta as _td
        year_end = min(date(y + 1, 1, 1), today + _td(days=1))
        after_dt = datetime.combine(year_start, datetime.min.time(), tzinfo=timezone.utc)
        before_dt = datetime.combine(year_end, datetime.min.time(), tzinfo=timezone.utc)
        out.append((y, per_bucket, after_dt, before_dt))
    return out


class Ingest(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- Écoute des nouveaux messages -------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # On ignore les MP et les messages non éligibles / opt-out / hors scope.
        if message.guild is None:
            return
        if message.author.id in config.BLACKLIST_USER_IDS:
            return
        if config.ALLOWED_CHANNEL_IDS and message.channel.id not in config.ALLOWED_CHANNEL_IDS:
            return
        if database.is_opted_out(message.author.id, message.guild.id):
            return
        if filters.is_eligible(message, config.MIN_CHARS, config.MIN_WORDS):
            author_name = getattr(message.author, "global_name", None) or message.author.name
            database.upsert_user(
                message.guild.id, message.author.id, author_name, _avatar_url(message.author)
            )
            database.add_message(
                message.id,
                message.guild.id,
                message.channel.id,
                message.author.id,
                author_name,
                message.content,
                message.created_at.isoformat(),
            )

    # --- Scan de l'historique --------------------------------------------

    @app_commands.command(
        name="backfill",
        description="Scanne l'historique des salons pour alimenter la base de messages.",
    )
    @app_commands.describe(
        limite="Nombre de messages à scanner par salon (défaut : valeur de config).",
        salon="Limiter le scan à un seul salon (facultatif).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def backfill(
        self,
        interaction: discord.Interaction,
        limite: int = 0,
        salon: Optional[discord.TextChannel] = None,
    ):
        if limite <= 0:
            limite = config.BACKFILL_LIMIT

        await interaction.response.defer(thinking=True, ephemeral=True)

        guild = interaction.guild
        me = guild.me
        channels = [salon] if salon else list(guild.text_channels)

        # Si une whitelist de salons est définie, on s'y restreint avant tout scan.
        if config.ALLOWED_CHANNEL_IDS:
            channels = [c for c in channels if c.id in config.ALLOWED_CHANNEL_IDS]
            if not channels:
                await interaction.followup.send(
                    "Aucun salon scanné : aucun ne correspond à `ALLOWED_CHANNEL_IDS`. "
                    "Vérifie ta configuration dans `.env`.",
                    ephemeral=True,
                )
                return

        total_added = 0
        scanned = 0
        skipped_channels = 0

        buckets = _year_buckets(limite)
        by_year_added = {y: 0 for y, *_ in buckets if y is not None}

        for channel in channels:
            perms = channel.permissions_for(me)
            if not (perms.read_messages and perms.read_message_history):
                skipped_channels += 1
                continue
            try:
                for year, bucket_size, after_dt, before_dt in buckets:
                    history_kwargs = {"limit": bucket_size}
                    if after_dt is not None:
                        history_kwargs["after"] = after_dt
                    if before_dt is not None:
                        history_kwargs["before"] = before_dt
                        history_kwargs["oldest_first"] = False
                    rows = []
                    profiles = {}
                    async for msg in channel.history(**history_kwargs):
                        scanned += 1
                        if msg.author.id in config.BLACKLIST_USER_IDS:
                            continue
                        if database.is_opted_out(msg.author.id, guild.id):
                            continue
                        if filters.is_eligible(msg, config.MIN_CHARS, config.MIN_WORDS):
                            author_name = (
                                getattr(msg.author, "global_name", None) or msg.author.name
                            )
                            profiles[msg.author.id] = (
                                author_name,
                                _avatar_url(msg.author),
                            )
                            rows.append(
                                (
                                    msg.id,
                                    guild.id,
                                    channel.id,
                                    msg.author.id,
                                    author_name,
                                    msg.content,
                                    msg.created_at.isoformat(),
                                )
                            )
                    added = database.add_messages_bulk(rows)
                    for author_id, (author_name, avatar_url) in profiles.items():
                        database.upsert_user(guild.id, author_id, author_name, avatar_url)
                    total_added += added
                    if year is not None:
                        by_year_added[year] += added
            except discord.Forbidden:
                skipped_channels += 1
                continue

        scope = salon.mention if salon else f"**{len(channels)}** salons"
        total_db = database.count_messages(guild.id)
        authors = database.count_authors(guild.id)

        year_lines = ""
        if by_year_added and len(by_year_added) > 1:
            breakdown = "\n".join(
                f"  • **{y}** : {by_year_added[y]} messages"
                for y in sorted(by_year_added)
            )
            year_lines = f"• Répartition par année :\n{breakdown}\n"

        await interaction.followup.send(
            f"✅ Backfill terminé sur {scope}.\n"
            f"• Messages scannés : **{scanned}**\n"
            f"• Nouveaux messages éligibles ajoutés : **{total_added}**\n"
            f"{year_lines}"
            f"• Salons ignorés (permissions) : **{skipped_channels}**\n"
            f"• Total en base : **{total_db}** messages de **{authors}** auteurs.",
            ephemeral=True,
        )

    @backfill.error
    async def backfill_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ Tu dois avoir la permission **Gérer le serveur** pour lancer un backfill."
        else:
            msg = f"❌ Une erreur est survenue : {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ingest(bot))
