"""API key generation and verification.

Format: ``ef_<env>_<public>_<secret>``

  ps_prod_a1b2c3d4_<43 url-safe base64 chars>
  └┬┘ └─┬┘ └───┬──┘ └──────────┬────────────┘
   │    │      │               └ 256 bits of entropy, never stored
   │    │      └ indexed lookup handle, stored in the clear
   │    └ environment, so a staging key is visibly not a production key
   └ fixed prefix, which is what makes the key detectable by secret scanners

**Hashed with SHA-256, deliberately not argon2 or bcrypt.** These are 256-bit random
secrets, not user-chosen passwords: there is no dictionary to attack and no
meaningful brute-force surface, so a deliberately slow KDF would add ~100ms to
*every ingest request* and buy nothing. Passwords, which are low-entropy and
guessable, use argon2id instead (see `passwords.py`). Applying the same primitive to
both would be a category error in one direction or the other.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

PREFIX = "ps"
SECRET_BYTES = 32
PUBLIC_BYTES = 4
_PARTS = 4


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """A freshly minted key. `token` is returned to the user exactly once."""

    token: str
    prefix: str
    key_hash: bytes

    @property
    def display_hint(self) -> str:
        return f"{self.prefix}_{'•' * 8}"


def generate(environment: str = "dev") -> GeneratedKey:
    env = _normalize_environment(environment)
    public = secrets.token_hex(PUBLIC_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    token = f"{PREFIX}_{env}_{public}_{secret}"
    return GeneratedKey(token=token, prefix=f"{PREFIX}_{env}_{public}", key_hash=hash_key(token))


def hash_key(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def parse_prefix(token: str) -> str | None:
    """Extract the public lookup handle, or None if the token is malformed.

    Parsing before any database work means a garbage Authorization header costs one
    string split rather than a query.

    The split is bounded to three, not unbounded. `token_urlsafe` emits base64url,
    whose alphabet includes `_`, so roughly half of all secrets contain one. An
    unbounded split saw those as five-part tokens and rejected them as malformed —
    a bug that would have made about half of every project's issued keys fail
    authentication, intermittently and with no obvious pattern.
    """
    parts = token.split("_", _PARTS - 1)
    if len(parts) != _PARTS or parts[0] != PREFIX:
        return None
    if not all(parts[1:]):
        return None
    return f"{parts[0]}_{parts[1]}_{parts[2]}"


def verify(token: str, expected_hash: bytes) -> bool:
    """Constant-time comparison, so response timing cannot leak the stored digest."""
    return hmac.compare_digest(hash_key(token), expected_hash)


#: Short forms for the environment names almost everyone uses.
#:
#: Without these, truncating to eight characters produces `ef_producti_…`, which looks like a typo
#: on the one string a user copies, pastes, and shows to colleagues. A key is a piece of product
#: surface, not just a credential.
_ENVIRONMENT_ALIASES = {
    "production": "prod",
    "prod": "prod",
    "staging": "stg",
    "stage": "stg",
    "development": "dev",
    "dev": "dev",
    "test": "test",
    "local": "local",
}


def _normalize_environment(environment: str) -> str:
    cleaned = "".join(ch for ch in environment.lower() if ch.isalnum())
    # A known name maps to its short form; anything else is truncated, because an environment called
    # `customer-acceptance-2` still has to produce a prefix that fits in a column.
    return _ENVIRONMENT_ALIASES.get(cleaned, cleaned[:8]) or "dev"
