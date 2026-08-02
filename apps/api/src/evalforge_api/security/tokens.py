"""JWT access tokens and rotating refresh tokens.

Access tokens are short-lived and stateless; refresh tokens are long-lived, stored
hashed, single-use, and grouped into families so that replay revokes everything.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from evalforge_api.errors import UnauthorizedError

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 — a claim value, not a credential


@dataclass(frozen=True, slots=True)
class AccessClaims:
    subject: str
    expires_at: datetime
    issued_at: datetime


def create_access_token(
    user_id: uuid.UUID | str, *, secret: str, ttl_s: int = 900
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_s)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "typ": ACCESS_TOKEN_TYPE,
        "jti": secrets.token_urlsafe(8),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM), expires


def decode_access_token(token: str, *, secret: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],  # pinned: never trust the token's own `alg`
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("The access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("The access token is not valid.") from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        # Without this, a refresh token would be accepted wherever an access token
        # is, quietly turning a 30-day credential into a 15-minute one's authority.
        raise UnauthorizedError("Wrong token type for this endpoint.")

    return AccessClaims(
        subject=str(payload["sub"]),
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        issued_at=datetime.fromtimestamp(payload["iat"], UTC),
    )


def generate_refresh_token() -> tuple[str, bytes]:
    """Return the opaque token and the digest to store.

    Opaque and random rather than a JWT: a refresh token must be revocable, and
    revoking a stateless token means keeping a denylist anyway.
    """
    token = secrets.token_urlsafe(32)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()
