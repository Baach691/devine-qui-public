"""Filtrage des messages : décide si un message est utilisable dans le jeu.

Un message est éligible si :
  - son auteur n'est pas un bot ;
  - il ne contient ni pièce jointe, ni embed (image/vidéo/aperçu de lien) ;
  - il ne contient pas de lien ;
  - ce n'est pas une commande (commence par !, /, ;, ?, ...) ;
  - une fois nettoyé (mentions, emojis, espaces retirés) il fait au moins
    MIN_CHARS caractères et MIN_WORDS mots.
"""

import re

# Liens (http/https ou www.) et invitations Discord
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
INVITE_RE = re.compile(r"discord(?:\.gg|(?:app)?\.com/invite)/\S+", re.IGNORECASE)

# Emojis personnalisés du serveur : <:nom:id> ou <a:nom:id>
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")

# Mentions : @membre, @&rôle, #salon
MENTION_RE = re.compile(r"<@[!&]?\d+>|<#\d+>")

# Emojis Unicode (plages principales) — approximation suffisante pour le filtrage
EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff"  # symboles, emojis, drapeaux...
    "\U00002600-\U000027bf"   # divers symboles et dingbats
    "\U00002190-\U000021ff"   # flèches
    "\U00002300-\U000023ff"   # symboles techniques
    "\U0000fe00-\U0000fe0f"   # sélecteurs de variation
    "\U0000200d]",            # zero-width joiner
    flags=re.UNICODE,
)

# Préfixes typiques de commandes de bots
COMMAND_PREFIXES = ("!", "/", ";", "?", ".", "-", "$", "+", ">", "=", "&", "%", "*")


_MEDIA_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".apng", ".bmp",
    ".mp4", ".mov", ".webm", ".mkv", ".m4v",
)


def media_attachment_url(message):
    """URL d'un média éligible (image/gif/vidéo réellement uploadé sur Discord).

    Renvoie None si le message n'a pas de média éligible. On ne prend QUE les
    vraies pièces jointes Discord (message.attachments) : les embeds, previews et
    cartes générées à partir de liens externes (Tenor, Twitter, YouTube…) sont
    donc automatiquement exclus. Exclut bots, webhooks et comptes supprimés.
    """
    if getattr(message.author, "bot", False):
        return None
    if getattr(message, "webhook_id", None):
        return None
    if is_deleted_user(message.author):
        return None
    for att in getattr(message, "attachments", []) or []:
        ct = (getattr(att, "content_type", None) or "").lower()
        fn = (getattr(att, "filename", "") or "").lower()
        if ct.startswith("image/") or ct.startswith("video/") or fn.endswith(_MEDIA_EXT):
            return att.url
    return None


def is_deleted_user(author) -> bool:
    """True si l'auteur est un compte Discord supprimé ("Deleted User").

    Discord renomme les comptes supprimés en `deleted_user_<hex>` (username) et
    leur affichage devient « Deleted User ». On détecte les deux formes.
    Accepte un objet user/member OU directement un nom (str).
    """
    if isinstance(author, str):
        candidates = [author]
    else:
        candidates = [
            getattr(author, "name", None),
            getattr(author, "global_name", None),
            getattr(author, "display_name", None),
        ]
    for raw in candidates:
        if not raw:
            continue
        low = raw.strip().lower()
        if low.startswith("deleted_user") or low == "deleted user":
            return True
    return False


def clean_content(content: str) -> str:
    """Retire mentions, emojis (custom + Unicode), liens et espaces superflus.

    Sert uniquement à *mesurer* la longueur réelle du texte ; le message
    affiché dans le jeu reste le message original.
    """
    text = CUSTOM_EMOJI_RE.sub("", content)
    text = MENTION_RE.sub("", text)
    text = URL_RE.sub("", text)
    text = EMOJI_RE.sub("", text)
    return " ".join(text.split())  # normalise les espaces


def is_eligible(message, min_chars: int, min_words: int) -> bool:
    """Renvoie True si le message peut servir dans le jeu.

    `message` est un discord.Message (issu de l'écoute en direct ou du backfill).
    """
    if message.author.bot:
        return False
    if is_deleted_user(message.author):  # comptes supprimés ("Deleted User")
        return False
    if message.attachments:  # images, vidéos, fichiers
        return False
    if message.embeds:  # aperçus de liens, embeds riches
        return False

    content = message.content or ""
    stripped = content.strip()
    if not stripped:
        return False
    if stripped.startswith(COMMAND_PREFIXES):
        return False
    if URL_RE.search(content) or INVITE_RE.search(content):
        return False

    cleaned = clean_content(content)
    if len(cleaned) < min_chars:
        return False
    if len(cleaned.split()) < min_words:
        return False

    return True
