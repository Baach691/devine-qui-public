"""Chargement de la configuration depuis le fichier .env."""

import os

from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    """Lit une variable d'environnement entière, avec valeur par défaut."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_int_list(name: str) -> list:
    """Lit une liste d'IDs depuis une env var (séparés par virgules).

    Exemple : ALLOWED_CHANNEL_IDS=111111111111111111,222222222222222222
    Vide ou non défini → liste vide.
    """
    raw = os.getenv(name, "")
    if not raw or not raw.strip():
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


TOKEN = os.getenv("DISCORD_TOKEN")

# ID du serveur de test (None = synchro globale des commandes slash)
_guild = os.getenv("GUILD_ID")
GUILD_ID = int(_guild) if _guild and _guild.strip() else None

DB_PATH = os.getenv("DB_PATH", "bot.db")

# Filtrage des messages
MIN_CHARS = _get_int("MIN_CHARS", 10)
MIN_WORDS = _get_int("MIN_WORDS", 2)

# Backfill
BACKFILL_LIMIT = _get_int("BACKFILL_LIMIT", 2000)

# Anti-répétition : nombre de derniers messages tirés à exclure du prochain tirage
RECENT_PICKS_EXCLUDE = _get_int("RECENT_PICKS_EXCLUDE", 100)

# Daily : fuseau horaire utilisé pour calculer "aujourd'hui" (reset à minuit)
DAILY_TIMEZONE = os.getenv("DAILY_TIMEZONE", "Europe/Paris")

# Daily : heure de pré-calcul du défi du jour (format HH:MM, dans DAILY_TIMEZONE).
# À cette heure-là, le bot tire et stocke le défi pour que le premier /daily soit
# instantané. Mettre vide (ou "off") pour désactiver le précalcul.
DAILY_PRECOMPUTE_TIME = os.getenv("DAILY_PRECOMPUTE_TIME", "00:00")

# Salon où envoyer l'annonce automatique du nouveau daily (avec ping des
# joueurs ayant tenté la veille). Si 0/vide, on retombe sur ALLOWED_CHANNEL_IDS,
# puis sur le salon du message tiré du jour.
ANNOUNCE_CHANNEL_ID = _get_int("ANNOUNCE_CHANNEL_ID", 0)

# Mode Hardcore : limite de temps en secondes pour répondre. Au-delà = défaite
# automatique. Le serveur applique une marge (latence réseau) en plus.
HARDCORE_TIME_LIMIT = _get_int("HARDCORE_TIME_LIMIT", 10)

# Tirage par date : on choisit une date au hasard entre OLDEST_MESSAGE_DATE et aujourd'hui,
# puis on cherche un message proche de cette date (fenêtre élargie progressivement).
# Format : YYYY-MM-DD. Défaut : 2023-10-01 (fin 2023).
OLDEST_MESSAGE_DATE = os.getenv("OLDEST_MESSAGE_DATE", "2023-10-01")

# Restrictions de tirage et d'ingestion :
# - ALLOWED_CHANNEL_IDS : si défini, seuls les messages de ces salons sont éligibles.
#   Vide = tous les salons. Format : IDs séparés par virgules.
# - BLACKLIST_USER_IDS  : auteurs jamais sélectionnés (ni comme bonne réponse,
#   ni comme distractor) et dont les messages ne sont jamais ingérés.
# - ALLOWED_ROLE_IDS    : whitelist par rôle Discord. Si défini, seuls les
#   membres ayant au moins un de ces rôles peuvent utiliser /daily et les
#   autres commandes du jeu. Vide = tout le monde peut jouer.
ALLOWED_CHANNEL_IDS = _get_int_list("ALLOWED_CHANNEL_IDS")
BLACKLIST_USER_IDS = _get_int_list("BLACKLIST_USER_IDS")
ALLOWED_ROLE_IDS = _get_int_list("ALLOWED_ROLE_IDS")
# Comptes Discord autorisés à corriger l'historique depuis le panneau web.
ADMIN_USER_IDS = _get_int_list("ADMIN_USER_IDS")

# Webapp : interface graphique servie par Flask.
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
WEBAPP_PORT = _get_int("WEBAPP_PORT", 8001)
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", f"http://{WEBAPP_HOST}:{WEBAPP_PORT}")
# Chaque connexion SSE occupe un thread Waitress. Ce pool dimensionne donc le
# nombre de viewers simultanés, avec une marge pour les requêtes classiques.
WEBAPP_THREADS = max(8, _get_int("WEBAPP_THREADS", 64))

# Compatibilité vidéo : les navigateurs qui ne décodent pas la piste d'origine
# demandent une version H.264/AAC générée par ffmpeg et mise en cache brièvement.
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
MEDIA_CACHE_DIR = os.getenv(
    "MEDIA_CACHE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), ".media_cache"),
)
MEDIA_CACHE_RETENTION_HOURS = max(
    1, _get_int("MEDIA_CACHE_RETENTION_HOURS", 48)
)
MEDIA_MAX_TRANSCODE_MB = max(1, _get_int("MEDIA_MAX_TRANSCODE_MB", 100))

# Clé secrète pour signer les liens. Priorité : .env. Sinon, on génère un secret
# UNE fois et on le PERSISTE dans un fichier local (.webapp_secret) → les liens
# survivent aux redémarrages même sans variable d'environnement.
WEBAPP_SECRET = os.getenv("WEBAPP_SECRET")
if not WEBAPP_SECRET:
    import logging as _logging
    import secrets as _secrets

    _log = _logging.getLogger(__name__)
    _secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".webapp_secret")
    try:
        with open(_secret_path, "r", encoding="utf-8") as _f:
            WEBAPP_SECRET = _f.read().strip() or None
    except OSError:
        WEBAPP_SECRET = None
    if not WEBAPP_SECRET:
        WEBAPP_SECRET = _secrets.token_urlsafe(32)
        try:
            with open(_secret_path, "w", encoding="utf-8") as _f:
                _f.write(WEBAPP_SECRET)
            os.chmod(_secret_path, 0o600)
            _log.warning(
                "WEBAPP_SECRET non défini dans .env : secret généré et persisté dans %s "
                "(les liens survivront aux redémarrages). Définis WEBAPP_SECRET dans .env "
                "si tu préfères le gérer toi-même.",
                _secret_path,
            )
        except OSError:
            _log.warning(
                "WEBAPP_SECRET non défini et impossible d'écrire %s : secret volatil "
                "(les liens casseront au redémarrage). Définis WEBAPP_SECRET dans .env.",
                _secret_path,
            )


# Discord Activity (Embedded App SDK) — app embarquée dans Discord.
# - CLIENT_ID = Application ID (PUBLIC). Aussi exposé au front via VITE_DISCORD_CLIENT_ID
#   (Vite n'expose au client que les variables préfixées VITE_).
# - CLIENT_SECRET = secret OAuth2 (NE DOIT JAMAIS être exposé au front).
# On lit VITE_DISCORD_CLIENT_ID en priorité (nom utilisé aussi par le build front),
# avec repli sur DISCORD_CLIENT_ID.
DISCORD_CLIENT_ID = os.getenv("VITE_DISCORD_CLIENT_ID") or os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
