"""The never-raise guarantee.

A telemetry library that can crash the host application is unusable, and this is
non-negotiable. Every public entry point is wrapped: an internal error is logged
once per window and the call returns a harmless value.

Logging is rate-limited for the same reason. A library that logs once per span
during an outage produces more damage than the outage — it fills the disk, floods
the aggregator, and buries the real error.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

logger = logging.getLogger("proofstep")

T = TypeVar("T")

# One message per (site, window). 60s is long enough that a sustained failure is
# visible roughly once a minute rather than once a span.
_LOG_WINDOW_S = 60.0
_last_logged: dict[str, float] = {}
_lock = threading.Lock()

_STRICT = os.environ.get("PROOFSTEP_STRICT", "").strip().lower() in ("1", "true", "yes")
"""Re-raise internal errors instead of swallowing them.

For our own test suite only. Swallowing bugs is correct in production and
catastrophic in tests, where it would let the SDK silently record nothing while
every assertion about behaviour still passed.
"""


def log_once(key: str, message: str, *, exc_info: bool = False) -> None:
    """Log at most once per key per window."""
    now = time.monotonic()
    with _lock:
        last = _last_logged.get(key)
        if last is not None and now - last < _LOG_WINDOW_S:
            return
        _last_logged[key] = now
    logger.warning("proofstep: %s", message, exc_info=exc_info)


def reset_log_throttle() -> None:
    """Test helper."""
    with _lock:
        _last_logged.clear()


def never_raises(default: Any = None, *, key: str | None = None) -> Callable[..., Any]:
    """Decorator: swallow every exception and return `default`.

    `BaseException` subclasses that are not `Exception` — KeyboardInterrupt,
    SystemExit, and crucially `asyncio.CancelledError` — are deliberately not
    caught. Swallowing cancellation would break the caller's control flow, which is
    the opposite of staying out of the way.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        site = key or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if _STRICT:
                    raise
                log_once(site, f"{site} failed: {type(exc).__name__}: {exc}", exc_info=True)
                return default() if callable(default) else default

        return wrapper

    return decorate


class _NoOp:
    """Returned when the SDK cannot produce a real object.

    Absorbs every attribute access, call, and context-manager use, so user code
    written against a working SDK keeps running against a broken one.
    """

    __slots__ = ()

    def __getattr__(self, _name: str) -> _NoOp:
        return self

    def __call__(self, *_args: Any, **_kwargs: Any) -> _NoOp:
        return self

    def __enter__(self) -> _NoOp:
        return self

    def __exit__(self, *_exc: object) -> Literal[False]:
        return False  # never suppress the caller's exception

    async def __aenter__(self) -> _NoOp:
        return self

    async def __aexit__(self, *_exc: object) -> Literal[False]:
        return False

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<proofstep disabled>"


NOOP = _NoOp()
