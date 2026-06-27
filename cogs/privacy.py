"""Respect de la vie privée : /optout et /optin."""

import discord
from discord import app_commands
from discord.ext import commands

import database


class Privacy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="optout",
        description="Ne plus apparaître dans le jeu : supprime tes messages de la base.",
    )
    @app_commands.guild_only()
    async def optout(self, interaction: discord.Interaction):
        deleted = database.opt_out(interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(
            f"🔒 C'est noté. **{deleted}** de tes messages ont été retirés de la base "
            "et tu n'apparaîtras plus dans le jeu. Utilise `/optin` pour revenir.",
            ephemeral=True,
        )

    @app_commands.command(
        name="optin",
        description="Réautoriser l'utilisation de tes messages dans le jeu.",
    )
    @app_commands.guild_only()
    async def optin(self, interaction: discord.Interaction):
        database.opt_in(interaction.user.id, interaction.guild_id)
        await interaction.response.send_message(
            "✅ Te revoilà dans le jeu ! Tes prochains messages éligibles seront pris en "
            "compte (et le prochain `/backfill` ré-importera les anciens).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Privacy(bot))
