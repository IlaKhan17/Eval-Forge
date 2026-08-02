"""Password hashing with argon2id.

Passwords are low-entropy and guessable, so the cost parameters exist to make each
guess expensive. This is the opposite trade-off from API keys, which are
high-entropy and hashed with plain SHA-256 (see `keys.py`).
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# OWASP-recommended baseline: 19 MiB, 2 iterations, 1 degree of parallelism.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

MIN_LENGTH = 12
MAX_LENGTH = 1024


class WeakPasswordError(ValueError):
    """The password does not meet the minimum policy."""


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        msg = f"password must be at least {MIN_LENGTH} characters"
        raise WeakPasswordError(msg)
    if len(password) > MAX_LENGTH:
        # Unbounded input to a memory-hard function is a denial-of-service vector.
        msg = f"password must be at most {MAX_LENGTH} characters"
        raise WeakPasswordError(msg)
    return _hasher.hash(password)


def verify_password(password: str, stored: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(stored: str) -> bool:
    """True when the stored hash used weaker parameters than the current policy."""
    try:
        return _hasher.check_needs_rehash(stored)
    except (InvalidHashError, ValueError):
        return False
