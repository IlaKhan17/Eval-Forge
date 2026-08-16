"""Proofstep Python SDK — tracing for AI applications and tool-using agents.

    import proofstep

    proofstep.init(project="my-app")

    @proofstep.trace("generate_outreach")
    async def generate_outreach(prospect_id: str) -> Email:
        ...

    @proofstep.tool("gmail.send")
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
from importlib import metadata as _metadata
from typing import Any

from proofstep import redaction
from proofstep.client import Captured, Client, sampled
from proofstep.config import Config
from proofstep.context import current_span, current_trace, propagate
from proofstep.decorators import make_span, make_tool, make_trace
from proofstep.propagation import extract, inject
from proofstep.recorder import SpanRecorder, TraceRecorder
from proofstep.safety import NOOP
from proofstep_types import CaptureMode, SpanType, Status, Trace

# Read from the installed distribution rather than written here twice. A hand-maintained
# copy drifts the first time a release bumps one and not the other — which it already did,
# reporting 0.1.0.dev0 from a 0.1.0 wheel.
__version__ = _metadata.version("proofstep")

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
            msg = f"unknown Proofstep setting {key!r}"
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

        with proofstep.capture("classify") as captured:
            result = await classify(example.input)
        return proofstep.Captured(output=result, trace=captured[0])
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
