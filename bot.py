"""Point d'entrée du bot Daily Guessr.

Lance avec :  python bot.py
"""

import asyncio
import logging
import threading

import discord
from discord.ext import commands

import config
import database
from webapp import server as webapp_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# Les modes phrase & média n'ont plus de cog (scheduler unique dans cogs.daily) ;
# leurs fonctions de tirage sont importées directement.
COGS = ("cogs.ingest", "cogs.daily", "cogs.privacy")

# Le message_content est un intent privilégié : à activer dans le Developer Portal.
intents = discord.Intents.default()
intents.message_content = True


class GuessBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        database.init_db()

        for ext in COGS:
            await self.load_extension(ext)
            log.info("Extension chargée : %s", ext)

        # Synchro des commandes slash.
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("%d commandes synchronisées sur le serveur %s", len(synced), config.GUILD_ID)
        else:
            try:
                synced = await self.tree.sync()
                log.info(
                    "%d commandes synchronisées globalement (peut prendre ~1h)",
                    len(synced),
                )
            except discord.HTTPException as exc:
                # Une Activity possède une commande PRIMARY_ENTRY_POINT (type 4)
                # que discord.py ne sait pas inclure dans son bulk sync. Discord
                # refuse alors l'opération pour empêcher sa suppression. Les
                # commandes déjà enregistrées restent intactes : on poursuit le
                # démarrage au lieu de rendre le bot inutilisable.
                if exc.code != 50240:
                    raise
                log.warning(
                    "Synchronisation globale ignorée pour préserver la commande "
                    "Entry Point de l'Activity (Discord 50240)."
                )

    async def on_ready(self):
        log.info("Connecté en tant que %s (id: %s)", self.user, self.user.id)
        log.info("Présent sur %d serveur(s).", len(self.guilds))


def _start_webapp_thread(bot: commands.Bot) -> None:
    """Lance Flask dans un thread démon pour qu'il vive à côté de discord.py.

    On passe l'instance du bot pour que la webapp puisse, au reveal, fetcher
    les messages voisins du daily directement via l'API Discord.
    """
    def _run():
        try:
            webapp_server.run(bot=bot)
        except Exception:
            log.exception("Le serveur web a planté.")

    t = threading.Thread(target=_run, name="webapp", daemon=True)
    t.start()
    log.info(
        "Interface web lancée sur %s (lien envoyé par /daily)", config.WEBAPP_BASE_URL
    )


def main():
    if not config.TOKEN:
        raise SystemExit(
            "❌ DISCORD_TOKEN manquant. Copie .env.example en .env et renseigne ton token."
        )
    # Init de la base avant de démarrer Flask ET le bot (les deux y accèdent).
    database.init_db()
    bot = GuessBot()
    _start_webapp_thread(bot)

    bot.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
