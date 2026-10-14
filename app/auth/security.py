import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt

from app.config import settings
from app.errors import AuthenticationError

_ALGORITHM = "pbkdf2_sha256"
_JWT_ALGORITHM = "HS256"

# Audience of REST API tokens. MCP tokens use the MCP resource URL instead, and calls
# between mesh nodes use MESH_AUDIENCE, so no kind of token is accepted where another
# is expected.
API_AUDIENCE = "meeting-notes-api"
MESH_AUDIENCE = "meeting-notes-mesh"


def hash_password(password: str) -> str:
    """PBKDF2-SHA256 (stdlib), stored as ``pbkdf2_sha256$<iterations>$<salt>$<hash>``."""
    iterations = settings.password_hash_iterations
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, hashed: str) -> bool:
    try:
        algorithm, iterations, salt, expected = hashed.split("$")
    except ValueError:
        return False
    if algorithm != _ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    return hmac.compare_digest(_b64(digest), expected)


def create_access_token(user_id: int, *, audience: str = API_AUDIENCE, ttl_minutes: Optional[float] = None,
                        claims: Optional[Dict[str, Any]] = None) -> str:
    now = datetime.now(timezone.utc)
    ttl = timedelta(minutes=settings.access_token_ttl_minutes if ttl_minutes is None else ttl_minutes)
    payload = {"sub": str(user_id), "aud": audience, "iat": now, "exp": now + ttl, **(claims or {})}
    return jwt.encode(payload, settings.secret_key, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str, audience: str = API_AUDIENCE) -> Dict[str, Any]:
    """Return the claims of a valid token for ``audience`` (``sub`` as an int), or raise
    ``AuthenticationError``."""
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[_JWT_ALGORITHM], audience=audience)
        claims["sub"] = int(claims["sub"])
        return claims
    except (jwt.PyJWTError, KeyError, ValueError):
        raise AuthenticationError("Invalid or expired token") from None


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def hash_opaque_token(token: str) -> str:
    """Opaque tokens are random and high-entropy, so an unsalted SHA-256 is enough to
    store them without keeping the usable value."""
    return hashlib.sha256(token.encode()).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()
