"""EvalForge Python SDK — tracing for AI applications and tool-using agents.

    import evalforge

    evalforge.init(project="my-app")

    @evalforge.trace("generate_outreach")
    async def generate_outreach(prospect_id: str) -> Email:
        ...

    @evalforge.tool("gmail.send")
    async def send_email(to: str, subject: str, body: str) -> str:
        ...

Two guarantees hold everywhere in this package:

- **It never raises into your application.** Every public entry point is wrapped;
  internal failures are logged once per window and return a no-op.
- **It never blocks your application.** Export is a non-blocking enqueue onto a
  bounded buffer drained by a background thread. When the buffer is full it drops
  the oldest trace and counts it, because visible loss beats an invisible stall.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from evalforge import redaction
from evalforge.client import Captured, Client, sampled
from evalforge.config import Config
from evalforge.context import current_span, current_trace, propagate
from evalforge.decorators import make_span, make_tool, make_trace
from evalforge.propagation import extract, inject
from evalforge.recorder import SpanRecorder, TraceRecorder
from evalforge.safety import NOOP
from evalforge_types import CaptureMode, SpanType, Status, Trace

__version__ = "0.1.0.dev0"

_client: Client | None = None


def init(**settings: Any) -> Client:
    """Configure the SDK. Safe to call more than once; the last call wins."""
    global _client  # noqa: PLW0603 — one process-wide client is the intended shape
    _client = Client(Config.from_env(**settings))
    return _client


def get_client() -> Client:
    """The active client, created from the environment on first use.

    Implicit initialization is deliberate: an unconfigured import must still work,
    so that adding a decorator never breaks a script that has not called `init`.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        _client = Client(Config.from_env())
    return _client


def configure(**settings: Any) -> None:
    """Update settings on the existing client without replacing it."""
    client = get_client()
    for key, value in settings.items():
        if not hasattr(client.config, key):
            msg = f"unknown EvalForge setting {key!r}"
            raise TypeError(msg)
        setattr(client.config, key, value)


def reset() -> None:
    """Drop the active client. For tests."""
    global _client  # noqa: PLW0603
    if _client is not None:
        _client.shutdown(0.1)
    _client = None


trace = make_trace(get_client)
span = make_span(get_client)
tool = make_tool(span)


@contextlib.contextmanager
def start_trace(name: str, **kwargs: Any) -> Iterator[Any]:
    """Context-manager form of `@trace`."""
    with get_client().trace(name, **kwargs) as recorder:
        yield recorder


@contextlib.contextmanager
def start_span(name: str, **kwargs: Any) -> Iterator[Any]:
    """Context-manager form of `@span`."""
    with get_client().span(name, **kwargs) as recorder:
        yield recorder


@contextlib.contextmanager
def capture(name: str = "task", **kwargs: Any) -> Iterator[list[Trace]]:
    """Record a trace and hand it back rather than only exporting it.

    This is how an instrumented task feeds the local evaluation engine: the captured
    `Trace` is what trajectory policies are evaluated against.

        with evalforge.capture("classify") as captured:
            result = await classify(example.input)
        return evalforge.Captured(output=result, trace=captured[0])
    """
    sink: list[Trace] = []
    with get_client().trace(name, **kwargs) as recorder:
        yield sink
        if recorder is not NOOP:
            sink.append(recorder.snapshot())


def set_metadata(**values: Any) -> None:
    if (active := current_trace()) is not None:
        active.set_metadata(**values)


def set_tags(**values: str) -> None:
    if (active := current_trace()) is not None:
        active.set_tags(**values)


def set_state(**values: Any) -> None:
    """Record explicit workflow state for `final_state` policy rules."""
    if (active := current_trace()) is not None:
        active.set_state(**values)


def record_event(name: str, **attributes: Any) -> None:
    if (active := current_span()) is not None:
        active.record_event(name, **attributes)


def set_attributes(**values: Any) -> None:
    if (active := current_span()) is not None:
        active.set_attributes(**values)


def flush(timeout: float | None = None) -> bool:
    return bool(get_client().flush(timeout))


def shutdown(timeout: float | None = None) -> None:
    get_client().shutdown(timeout)


__all__ = [
    "NOOP",
    "CaptureMode",
    "Captured",
    "Client",
    "Config",
    "SpanRecorder",
    "SpanType",
    "Status",
    "Trace",
    "TraceRecorder",
    "capture",
    "configure",
    "current_span",
    "current_trace",
    "extract",
    "flush",
    "get_client",
    "init",
    "inject",
    "propagate",
    "record_event",
    "redaction",
    "reset",
    "sampled",
    "set_attributes",
    "set_metadata",
    "set_state",
    "set_tags",
    "shutdown",
    "span",
    "start_span",
    "start_trace",
    "tool",
    "trace",
]
