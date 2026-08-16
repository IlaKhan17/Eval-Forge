"""Decorators — the lowest-friction way to instrument existing code.

Each inspects the wrapped function and returns a matching sync or async wrapper, so
the same decorator works on both without the user thinking about it.

`tool` is deliberately separate from `span`. It sets `span_type=tool`, records the
tool name, and captures the call arguments — which is precisely what the trajectory
engine consumes. Making it an obvious, distinct decorator is what turns policies from
a configuration exercise into a one-line change.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from proofstep.safety import NOOP
from proofstep_types import SpanType

if TYPE_CHECKING:
    from proofstep.client import Client

F = TypeVar("F", bound=Callable[..., Any])


class Decorator(Protocol):
    """A decorator factory that preserves the wrapped function's signature.

    Positional-or-keyword `name` plus arbitrary keywords: `span` accepts several
    extra keyword-only options that callers may pass through.
    """

    def __call__(self, name: str | None = ..., /, **kwargs: Any) -> Callable[[F], F]: ...


class SpanDecorator(Protocol):
    """`span` exposes its options explicitly rather than behind `**kwargs`.

    A typo in `capture_args` should be a type error, not a silently ignored keyword.
    """

    def __call__(
        self,
        name: str | None = ...,
        /,
        *,
        span_type: SpanType | str = ...,
        tool_name: str | None = ...,
        capture_args: bool = ...,
        capture_result: bool = ...,
    ) -> Callable[[F], F]: ...


# Arguments that are never worth capturing: they are either huge or self-referential.
_SKIP_ARGS = frozenset({"self", "cls"})


def _bind_args(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Best-effort mapping of a call to a plain dict for `tool_args`."""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return {k: v for k, v in bound.arguments.items() if k not in _SKIP_ARGS}
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": kwargs}


def make_trace(client_of: Callable[[], Client]) -> Decorator:
    def trace(name: str | None = None, /, **trace_kwargs: Any) -> Callable[[F], F]:
        def decorate(fn: F) -> F:
            label = name or fn.__name__

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    with client_of().trace(label, **trace_kwargs) as recorder:
                        result = await fn(*args, **kwargs)
                        if recorder is not NOOP:
                            recorder.set_metadata(**trace_kwargs.get("metadata", {}))
                        return result

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with client_of().trace(label, **trace_kwargs):
                    return fn(*args, **kwargs)

            return sync_wrapper  # type: ignore[return-value]

        return decorate

    return trace


def make_span(client_of: Callable[[], Client]) -> SpanDecorator:
    def span(
        name: str | None = None,
        /,
        *,
        span_type: SpanType | str = SpanType.CUSTOM,
        tool_name: str | None = None,
        capture_args: bool = False,
        capture_result: bool = True,
    ) -> Callable[[F], F]:
        def decorate(fn: F) -> F:
            label = name or fn.__name__

            def before(recorder: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
                if recorder is NOOP:
                    return
                bound = _bind_args(fn, args, kwargs)
                if capture_args:
                    recorder.set_args(bound)
                recorder.set_input(bound)

            def after(recorder: Any, result: Any) -> None:
                if recorder is not NOOP and capture_result:
                    recorder.set_output(result)

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    with client_of().span(label, span_type=span_type, tool_name=tool_name) as rec:
                        before(rec, args, kwargs)
                        result = await fn(*args, **kwargs)
                        after(rec, result)
                        return result

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                with client_of().span(label, span_type=span_type, tool_name=tool_name) as rec:
                    before(rec, args, kwargs)
                    result = fn(*args, **kwargs)
                    after(rec, result)
                    return result

            return sync_wrapper  # type: ignore[return-value]

        return decorate

    return span


def make_tool(span_factory: SpanDecorator) -> Decorator:
    def tool(name: str | None = None, /, **kwargs: Any) -> Callable[[F], F]:
        """Instrument a tool call. Captures arguments, which policies read."""

        def decorate(fn: F) -> F:
            label = name or fn.__name__
            decorated: F = span_factory(
                label,
                span_type=SpanType.TOOL,
                tool_name=label,
                capture_args=True,
                **kwargs,
            )(fn)
            return decorated

        return decorate

    return tool
