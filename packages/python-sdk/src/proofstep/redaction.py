"""Redaction, applied in-process before anything is exported.

This is the strongest privacy guarantee available: redacted data never leaves the
host process, so it cannot leak from our storage, our backups, or our logs. The
server-side pass is defence in depth, not the primary control (docs/SECURITY.md §4).

Secrets are redacted in **every** capture mode, including `full`. There is no
configuration in which Proofstep intentionally stores an API key.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from typing import Any

from proofstep_types import CaptureMode

Redactor = Callable[[str, Any], Any]

KEEP = object()
"""Sentinel: this redactor has no opinion; try the next one."""

MAX_DEPTH = 12
MAX_ITEMS = 1000

# Substring match on the key, case-insensitive. Deliberately broad: a false
# positive costs one obscured field, a false negative costs a credential.
SECRET_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "cookie",
    "session_id",
    "sessionid",
    "private_key",
    "client_secret",
    "credential",
    "auth",
)

PII_KEY_PARTS = ("ssn", "social_security", "credit_card", "card_number", "cvv", "iban")

# Value patterns. Ordered most to least specific.
SECRET_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("proofstep_key", re.compile(r"\bef_[a-z]+_[A-Za-z0-9]{4,}_[A-Za-z0-9]{16,}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.IGNORECASE)),
)

PII_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
)

ENTROPY_MIN_LENGTH = 32
ENTROPY_THRESHOLD = 4.0
_ENTROPY_CHARSET = re.compile(r"^[A-Za-z0-9+/=_-]+$")


def mask(reason: str) -> str:
    return f"[REDACTED:{reason}]"


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def looks_like_a_secret(value: str) -> bool:
    """High-entropy blob heuristic for credentials no pattern matches.

    Only fires on long, dense, base64/hex-ish strings. Prose does not reach 4.0
    bits per character, so ordinary model output is not caught by this.
    """
    if len(value) < ENTROPY_MIN_LENGTH or not _ENTROPY_CHARSET.match(value):
        return False
    return shannon_entropy(value) >= ENTROPY_THRESHOLD


# --------------------------------------------------------------------- redactors


def secret_keys() -> Redactor:
    def redactor(path: str, _value: Any) -> Any:
        leaf = path.rsplit(".", 1)[-1].lower()
        if any(part in leaf for part in SECRET_KEY_PARTS):
            return mask("secret_key")
        return KEEP

    return redactor


def secret_values() -> Redactor:
    def redactor(_path: str, value: Any) -> Any:
        if not isinstance(value, str):
            return KEEP
        for name, pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                # Substitute rather than drop the whole field: an error message
                # containing a token is still useful once the token is gone.
                return pattern.sub(mask(name), value)
        if looks_like_a_secret(value):
            return mask("high_entropy")
        # A credential no pattern knows about is often pasted *into* a sentence
        # ("the token is <blob>"), where a whole-field entropy check never fires.
        # Scan word by word so an unknown key shape is still caught in context.
        return _redact_high_entropy_words(value)

    return redactor


def _redact_high_entropy_words(value: str) -> Any:
    if len(value) < ENTROPY_MIN_LENGTH:
        return KEEP
    words = value.split()
    if len(words) < 2:
        return KEEP  # already handled by the whole-field check
    replaced = [mask("high_entropy") if looks_like_a_secret(w) else w for w in words]
    return " ".join(replaced) if replaced != words else KEEP


def pii() -> Redactor:
    def redactor(path: str, value: Any) -> Any:
        leaf = path.rsplit(".", 1)[-1].lower()
        if any(part in leaf for part in PII_KEY_PARTS):
            return mask("pii_key")
        if isinstance(value, str):
            for name, pattern in PII_VALUE_PATTERNS:
                if pattern.search(value):
                    return pattern.sub(mask(name), value)
        return KEEP

    return redactor


def keys(names: Iterable[str]) -> Redactor:
    wanted = {n.lower() for n in names}

    def redactor(path: str, _value: Any) -> Any:
        leaf = path.rsplit(".", 1)[-1].lower()
        return mask("configured") if leaf in wanted else KEEP

    return redactor


def regex(pattern: str, *, replacement: str = "[REDACTED:custom]") -> Redactor:
    compiled = re.compile(pattern)

    def redactor(_path: str, value: Any) -> Any:
        if isinstance(value, str) and compiled.search(value):
            return compiled.sub(replacement, value)
        return KEEP

    return redactor


def default() -> list[Redactor]:
    """Secrets only. PII redaction is opt-in because it has false positives.

    An email address in an SDR tool's payload is the *subject* of the workload, not
    an accident, and blanket-redacting it would make traces useless. Secrets have no
    such excuse.
    """
    return [secret_keys(), secret_values()]


# ---------------------------------------------------------------------- pipeline


class RedactionPipeline:
    def __init__(
        self,
        redactors: list[Redactor] | None = None,
        *,
        capture_mode: CaptureMode = CaptureMode.REDACTED,
        max_field_bytes: int = 256 * 1024,
    ) -> None:
        self.capture_mode = capture_mode
        self.max_field_bytes = max_field_bytes
        # Even in `full` mode the secret redactors run. There is no configuration in
        # which we intentionally store a credential.
        self.redactors = (
            [secret_keys(), secret_values()]
            if capture_mode is CaptureMode.FULL
            else (redactors if redactors is not None else default())
        )
        self.count = 0
        self.truncated = 0

    def apply(self, value: Any, *, path: str = "") -> Any:
        self.count = 0
        self.truncated = 0
        if not self.capture_mode.stores_payloads:
            return None
        return self._walk(value, path, 0)

    def _walk(self, value: Any, path: str, depth: int) -> Any:
        if depth > MAX_DEPTH:
            return "[TRUNCATED:depth]"

        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for i, (key, item) in enumerate(value.items()):
                if i >= MAX_ITEMS:
                    out["_truncated"] = f"{len(value) - MAX_ITEMS} more keys"
                    break
                child = f"{path}.{key}" if path else str(key)
                out[str(key)] = self._walk(item, child, depth + 1)
            return out

        if isinstance(value, list | tuple):
            items = list(value)[:MAX_ITEMS]
            walked = [self._walk(v, f"{path}[{i}]", depth + 1) for i, v in enumerate(items)]
            if len(value) > MAX_ITEMS:
                walked.append(f"[TRUNCATED:{len(value) - MAX_ITEMS} more items]")
            return walked

        return self._leaf(value, path)

    def _leaf(self, value: Any, path: str) -> Any:
        for redactor in self.redactors:
            result = redactor(path, value)
            if result is KEEP:
                continue
            if result != value:
                self.count += 1
            value = result
            if isinstance(value, str) and value.startswith("[REDACTED:"):
                return value

        if isinstance(value, str) and len(value.encode("utf-8", "ignore")) > self.max_field_bytes:
            self.truncated += 1
            # Record the loss rather than silently shortening: a truncated payload
            # that looks complete is how people debug the wrong thing for an hour.
            return {
                "_truncated": True,
                "_original_bytes": len(value.encode("utf-8", "ignore")),
                "_preview": value[:512],
            }
        return value
