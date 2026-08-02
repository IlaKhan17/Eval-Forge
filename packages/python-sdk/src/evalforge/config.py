"""SDK configuration.

Precedence: explicit arguments, then environment variables, then defaults. Env vars
matter more than they look — they are how a deployment turns capture down, or off,
without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from evalforge_types import CaptureMode

DEFAULT_ENDPOINT = "http://localhost:8000"

# Deliberately conservative. A telemetry library that quietly buffers unbounded
# work, or ships megabyte payloads by default, becomes the outage.
DEFAULT_MAX_BUFFERED_SPANS = 10_000
DEFAULT_BATCH_SIZE = 512
DEFAULT_FLUSH_INTERVAL_S = 2.0
DEFAULT_MAX_FIELD_BYTES = 256 * 1024
DEFAULT_MAX_SPAN_BYTES = 1024 * 1024
DEFAULT_MAX_BATCH_BYTES = 5 * 1024 * 1024


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


@dataclass
class Config:
    """Resolved SDK settings."""

    api_key: str | None = None
    endpoint: str = DEFAULT_ENDPOINT
    project: str | None = None
    environment: str = "development"
    capture_mode: CaptureMode = CaptureMode.REDACTED
    sample_rate: float = 1.0
    always_sample_on_error: bool = True

    enabled: bool = True
    export: bool = True
    """When False the SDK records in-process but never sends. This is what `--local`
    uses, and what makes the whole engine usable before anyone has an account."""

    max_buffered_spans: int = DEFAULT_MAX_BUFFERED_SPANS
    batch_size: int = DEFAULT_BATCH_SIZE
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S
    max_field_bytes: int = DEFAULT_MAX_FIELD_BYTES
    max_span_bytes: int = DEFAULT_MAX_SPAN_BYTES
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES
    max_spans_per_trace: int = 10_000

    export_timeout_s: float = 10.0
    max_retries: int = 5
    shutdown_timeout_s: float = 5.0

    spool_dir: Path | None = None
    """Where to persist batches the API refused. Off by default; on in CI, where a
    lost run is a lost signal rather than a monitoring gap."""

    git_commit: str | None = None
    service_name: str | None = None
    redact_keys: list[str] = field(default_factory=list)
    debug: bool = False

    @classmethod
    def from_env(cls, **overrides: object) -> Config:
        """Build a config from the environment, then apply explicit overrides."""
        capture_raw = os.environ.get("EVALFORGE_CAPTURE_MODE", "").strip().lower()
        try:
            capture = CaptureMode(capture_raw) if capture_raw else CaptureMode.REDACTED
        except ValueError:
            capture = CaptureMode.REDACTED

        spool = os.environ.get("EVALFORGE_SPOOL_DIR")

        config = cls(
            api_key=os.environ.get("EVALFORGE_API_KEY"),
            endpoint=os.environ.get("EVALFORGE_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/"),
            project=os.environ.get("EVALFORGE_PROJECT"),
            environment=os.environ.get("EVALFORGE_ENVIRONMENT", "development"),
            capture_mode=capture,
            sample_rate=_env_float("EVALFORGE_SAMPLE_RATE", 1.0),
            enabled=_env_bool("EVALFORGE_ENABLED", default=True),
            export=_env_bool("EVALFORGE_EXPORT", default=True),
            max_buffered_spans=_env_int("EVALFORGE_MAX_BUFFERED_SPANS", DEFAULT_MAX_BUFFERED_SPANS),
            batch_size=_env_int("EVALFORGE_BATCH_SIZE", DEFAULT_BATCH_SIZE),
            flush_interval_s=_env_float("EVALFORGE_FLUSH_INTERVAL", DEFAULT_FLUSH_INTERVAL_S),
            spool_dir=Path(spool) if spool else None,
            git_commit=os.environ.get("EVALFORGE_GIT_COMMIT") or _git_sha(),
            service_name=os.environ.get("EVALFORGE_SERVICE_NAME"),
            debug=_env_bool("EVALFORGE_DEBUG", default=False),
        )

        for key, value in overrides.items():
            if value is None:
                continue
            if not hasattr(config, key):
                msg = f"unknown EvalForge setting {key!r}"
                raise TypeError(msg)
            setattr(config, key, value)

        config.capture_mode = CaptureMode(config.capture_mode)
        config.sample_rate = min(1.0, max(0.0, config.sample_rate))
        return config

    @property
    def records(self) -> bool:
        return self.enabled and self.capture_mode is not CaptureMode.DISABLED

    @property
    def sends(self) -> bool:
        return self.records and self.export and bool(self.api_key)

    @property
    def stores_payloads(self) -> bool:
        return self.capture_mode.stores_payloads


def _git_sha() -> str | None:
    """Read the commit from CI environment variables only.

    Deliberately does not shell out to git: a telemetry import must not fork a
    subprocess, and in a container the repo usually is not there anyway.
    """
    for name in ("GITHUB_SHA", "GIT_COMMIT", "CI_COMMIT_SHA", "VERCEL_GIT_COMMIT_SHA"):
        if value := os.environ.get(name):
            return value
    return None
