"""HMAC-signed pagination cursors.

Keyset pagination, never OFFSET: `OFFSET 10000` on the trace table is a full scan,
and the trace list is the most-hit endpoint in the product.

The cursor is signed because it encodes a position the server will trust. An
unsigned cursor is a client-controlled query parameter that reaches the WHERE
clause, which is how an ordinary pagination bug becomes a cross-tenant read.
"""

from __future__ import annotations

import base64
import hmac
import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any


class InvalidCursorError(ValueError):
    """The cursor was malformed, tampered with, or signed by a different key."""


def encode(payload: dict[str, Any], *, secret: str) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_serialize)
    raw = body.encode("utf-8")
    signature = _sign(raw, secret)
    return _b64(raw) + "." + _b64(signature)


def decode(cursor: str, *, secret: str) -> dict[str, Any]:
    try:
        body_b64, signature_b64 = cursor.split(".", 1)
        raw = _unb64(body_b64)
        signature = _unb64(signature_b64)
    except (ValueError, TypeError) as exc:
        msg = "cursor is malformed"
        raise InvalidCursorError(msg) from exc

    if not hmac.compare_digest(signature, _sign(raw, secret)):
        # Do not say *how* it failed. "Bad signature" tells a prober they are close.
        msg = "cursor is not valid"
        raise InvalidCursorError(msg)

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = "cursor is malformed"
        raise InvalidCursorError(msg) from exc

    if not isinstance(decoded, dict):
        msg = "cursor is malformed"
        raise InvalidCursorError(msg)
    return decoded


def _sign(raw: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), raw, sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _serialize(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)
