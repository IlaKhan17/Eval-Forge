"""Context propagation.

`contextvars`, not thread-locals. `asyncio.create_task` and `TaskGroup` copy the
current context automatically, so spans created inside concurrent children attach to
the right parent with no user action — which is the whole reason to use contextvars
here, since an agent framework spawns tasks constantly.

Threads are the exception: Python does not copy contextvars across them, so
`propagate()` is provided and its necessity is documented rather than hidden.
"""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from evalforge.recorder import SpanRecorder, TraceRecorder

T = TypeVar("T")

_current_trace: contextvars.ContextVar[TraceRecorder | None] = contextvars.ContextVar(
    "evalforge_trace", default=None
)
_current_span: contextvars.ContextVar[SpanRecorder | None] = contextvars.ContextVar(
    "evalforge_span", default=None
)


def current_trace() -> TraceRecorder | None:
    return _current_trace.get()


def current_span() -> SpanRecorder | None:
    return _current_span.get()


def set_trace(trace: TraceRecorder | None) -> contextvars.Token[TraceRecorder | None]:
    return _current_trace.set(trace)


def set_span(span: SpanRecorder | None) -> contextvars.Token[SpanRecorder | None]:
    return _current_span.set(span)


def reset_trace(token: contextvars.Token[TraceRecorder | None]) -> None:
    _current_trace.reset(token)


def reset_span(token: contextvars.Token[SpanRecorder | None]) -> None:
    _current_span.reset(token)


def propagate(fn: Callable[..., T]) -> Callable[..., T]:  # noqa: UP047 — SDK targets py3.10
    """Carry the current context into a callable that will run on another thread.

    `ThreadPoolExecutor` does not copy contextvars, so a span created inside a
    worker would otherwise attach to nothing and appear as an orphan.

        pool.submit(evalforge.propagate(do_work), arg)
    """
    context = contextvars.copy_context()

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        return context.run(fn, *args, **kwargs)

    return wrapper


def snapshot() -> tuple[TraceRecorder | None, SpanRecorder | None]:
    return _current_trace.get(), _current_span.get()
