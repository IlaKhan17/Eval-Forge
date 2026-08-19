"""Password reset tokens, and the one route by which they leave this process.

Delivery is a seam with a single implementation, and that is a deliberate stopping point rather
than an unfinished one. There is no mail transport in this system yet — no SMTP, no provider — and
the wrong way to cope with that is to return the reset link in the HTTP response so the page can
show it. That turns "I forgot my password" into "type any address, receive an account". It is the
single most tempting shortcut in this whole flow and it hands over every account in the database.

So the token goes to the application log, at WARNING, marked for what it is. That is not a good
password-reset experience; it is an honest one. Whoever runs the installation can read it and pass
it on, which is a manual step with an audit trail, and `scripts/reset_link.py` exists so they can
mint one directly without the user having to ask through a form. When a mailer arrives, it
implements `deliver` and nothing else in the codebase changes.

The token itself is a URL-safe 256-bit random string, hashed with SHA-256 before it touches the
database — same construction as a refresh token and an invitation, for the same reason. Not Argon2,
which is for low-entropy secrets a human chose; a 256-bit random value has nothing to brute-force
and a slow hash here would only slow the legitimate path.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proofstep_api.settings import Settings

logger = logging.getLogger(__name__)


def generate() -> tuple[str, bytes]:
    """Return the token to send and the digest to store."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def reset_url(token: str, *, settings: Settings) -> str:
    """Where the token is redeemed.

    `dashboard_url` is configuration rather than something derived from the request, because a
    reset link built from a caller-supplied Host header is a redirect to whatever host the caller
    named — with a live token attached.
    """
    return f"{settings.dashboard_url.rstrip('/')}/reset?token={token}"


async def deliver(email: str, token: str, *, settings: Settings) -> None:
    """Get the reset link to the person who asked for it.

    Async because the mail transport that replaces this will be, and changing the signature later
    would mean touching the caller — which is the endpoint whose security properties are the ones
    worth not disturbing.
    """
    logger.warning(
        "PASSWORD RESET LINK for %s (no mail transport is configured, so this log is the "
        "delivery mechanism — see security/resets.py): %s",
        email,
        reset_url(token, settings=settings),
    )
