"""Per-credential rate limiting.

The settings for this have existed since the first API commit and nothing enforced them, which is
the worst of both worlds: a limit that is documented, configurable, and absent. A misconfigured SDK
retry loop — the single most likely source of load on an ingestion endpoint — could saturate the API
for every tenant on the deployment.

Three decisions worth stating, because each could reasonably have gone the other way:

**Buckets are keyed on the *validated* credential, never on the header.** Bucketing on the raw
`Authorization` value would let anyone burn another tenant's quota by sending their key prefix
with a wrong secret — a denial of service handed out for free. So limiting happens after
authentication, and requests that fail to authenticate are counted against the client address
instead, which is what makes key-guessing floods expensive.

**A fixed window, not a sliding log.** `INCR` plus `EXPIRE` is two round trips and O(1) memory; a
sliding window needs a sorted set per credential and a trim on every request. The cost of the
simpler choice is that a caller can send up to twice the limit across a window boundary, which for
limits whose purpose is "stop a runaway loop" is not a meaningful difference.

**It fails open.** If Redis cannot be reached the request is allowed, with a warning and a metric.
A rate limiter that takes the API down when its own dependency blips has caused a worse outage
than the abuse it exists to prevent — and this deployment already treats Redis as optional for the
read path. The failure is visible (`evalforge_rate_limiter_available`), so "we are not limiting
right now" is something an operator can see rather than assume.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("evalforge.ratelimit")

#: Window length. One minute because every limit in settings is expressed per minute, and a window
#: that does not match the unit people configure is a limit nobody can reason about.
WINDOW_S = 60

#: Endpoint classes. Ingestion is by far the highest-volume path and gets its own budget so a busy
#: exporter cannot starve the reads a dashboard needs to show what it is doing.
INGEST = "ingest"
READ = "read"
WRITE = "write"
AUTH = "auth"

#: Paths that are never limited.
#:
#: Throttling your own observability during an incident is exactly backwards: a scrape is a fixed,
#: low-rate machine call, and the moment a tenant's key is being throttled is the moment someone
#: most needs the metrics to say why. Found by watching /metrics return 429 while testing the
#: limiter — the failure would have shown up in production as a monitoring blackout that coincided
#: with every load spike.
EXEMPT_PATHS = ("/metrics", "/healthz", "/readyz")


class Counter(Protocol):
    """The two operations a fixed window needs, so a test can supply them without a Redis."""

    async def incr(self, key: str) -> int: ...

    async def expire(self, key: str, seconds: int) -> Any: ...


@dataclass(frozen=True)
class Decision:
    """The outcome of one check, including what to tell the caller.

    `limit` and `remaining` are returned even when the request is allowed, because a client that can
    see itself approaching a limit can slow down before it is refused — and a limiter that only
    speaks when it says no gives a caller no way to behave well.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_in: int
    #: False when the limiter could not reach its backend. The request was allowed regardless; this
    #: is how that shows up in metrics rather than looking like a quiet period.
    available: bool = True


class RateLimiter:
    """Fixed-window counter over any backend that can `INCR` and `EXPIRE`."""

    def __init__(self, counter: Counter | None, *, window_s: int = WINDOW_S) -> None:
        self._counter = counter
        self._window_s = window_s
        #: Set when a backend call fails, so `/metrics` can report that limiting is not in effect.
        self.available = counter is not None

    def backend(self) -> Any:
        """The underlying client, so the caller that created it can close it.

        Exposed rather than closed here because ownership belongs to whoever opened the connection —
        a limiter that closed a pool it did not create would surprise a second user of it.
        """
        return self._counter

    def _window_key(self, bucket: str, klass: str, now: float) -> str:
        # The window number is part of the key, so expiry is a safety net rather than the mechanism.
        # A key whose EXPIRE was lost still stops counting the moment the window rolls.
        window = int(now // self._window_s)
        return f"evalforge:rl:{klass}:{bucket}:{window}"

    async def check(self, bucket: str, klass: str, limit: int) -> Decision:
        """Count one request against a bucket and say whether it may proceed.

        `limit <= 0` disables the class outright. That is a real configuration — a self-hosted
        deployment with one trusted client has no use for limits — and it is cheaper to answer here
        than to make every caller special-case it.
        """
        if limit <= 0:
            return Decision(allowed=True, limit=limit, remaining=limit, reset_in=0)

        now = time.time()
        reset_in = self._window_s - int(now % self._window_s)

        if self._counter is None:
            return Decision(True, limit, limit, reset_in, available=False)

        key = self._window_key(bucket, klass, now)
        try:
            used = await self._counter.incr(key)
            if used == 1:
                # Only on the first hit of a window. Re-expiring on every request would extend the
                # window under sustained load, which turns a fixed window into a rolling ban.
                await self._counter.expire(key, self._window_s * 2)
        except Exception:  # fail open — see the module docstring
            if self.available:
                logger.warning("rate limiter backend unavailable; requests are not being limited")
            self.available = False
            return Decision(True, limit, limit, reset_in, available=False)

        self.available = True
        remaining = max(0, limit - used)
        return Decision(allowed=used <= limit, limit=limit, remaining=remaining, reset_in=reset_in)


def is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS


def classify(method: str, path: str) -> str:
    """Which budget a request draws on.

    Path-prefix matching rather than a decorator on each route: a new ingestion endpoint should be
    limited as ingestion by default, and the failure mode of forgetting a decorator is an unlimited
    endpoint — exactly the state this module exists to leave behind.
    """
    if path.startswith(("/v1/ingest", "/v1/otlp")):
        return INGEST
    return READ if method in ("GET", "HEAD") else WRITE


def limit_for(settings: Any, klass: str) -> int:
    limits: dict[str, int] = {
        INGEST: settings.rate_limit_ingest_per_min,
        READ: settings.rate_limit_read_per_min,
        WRITE: settings.rate_limit_write_per_min,
        AUTH: settings.rate_limit_auth_per_min,
    }
    return limits[klass]


def headers(decision: Decision) -> dict[str, str]:
    """Response headers describing the caller's remaining budget.

    The `X-RateLimit-*` spelling rather than the newer `RateLimit-*` draft, because every client
    library in this space already reads the former.
    """
    return {
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Reset": str(decision.reset_in),
    }


__all__ = [
    "AUTH",
    "EXEMPT_PATHS",
    "INGEST",
    "READ",
    "WINDOW_S",
    "WRITE",
    "Counter",
    "Decision",
    "RateLimiter",
    "classify",
    "headers",
    "is_exempt",
    "limit_for",
]
