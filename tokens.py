"""Tokens signés (HMAC-SHA256) pour authentifier les liens daily.

Format : <payload_base64url>.<signature_base64url>
Le payload est un JSON contenant guild_id, user_id, date, display_name, avatar_url.
La signature garantit que personne ne peut forger un lien avec une autre identité.
"""

import base64
import hashlib
import hmac
import json
from typing import Dict, Optional


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def make_token(payload: Dict, secret: str) -> str:
    """Encode un payload JSON et le signe avec HMAC-SHA256."""
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    data = _b64encode(raw)
    sig = hmac.new(secret.encode("utf-8"), data.encode("ascii"), hashlib.sha256).digest()
    return f"{data}.{_b64encode(sig)}"


def verify_token(token: str, secret: str) -> Optional[Dict]:
    """Renvoie le payload si la signature est valide, sinon None."""
    if not token or "." not in token:
        return None
    try:
        data, sig = token.rsplit(".", 1)
    except ValueError:
        return None

    expected_sig = hmac.new(
        secret.encode("utf-8"), data.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64encode(expected_sig), sig):
        return None

    try:
        return json.loads(_b64decode(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
