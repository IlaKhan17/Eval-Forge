"""The client: owns configuration, sampling, the exporter, and span creation."""

from __future__ import annotations

import atexit
import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from evalforge import context as ctx
from evalforge.config import Config
from evalforge.exporter import Exporter
from evalforge.recorder import SpanRecorder, TraceRecorder, new_trace_id
from evalforge.safety import NOOP, log_once, never_raises
from evalforge_types import SpanType, Status, Trace


@dataclass(frozen=True, slots=True)
class Captured:
    """A task's return value paired with the trace it produced.

    Shaped to match what the evaluation runner already looks for (`.output` and
    `.trace`), so an instrumented task drops into a suite with no adapter and no
    dependency from the engine back to the SDK.
    """

    output: Any
    trace: Trace


def sampled(trace_id: str, rate: float) -> bool:
    """Deterministic head sampling on the trace id.

    Hash-mod rather than a coin flip per span, so a sampled trace is captured
    *whole*. A half-recorded trajectory is worse than none: the policy engine would
    read the gaps as evidence.
    """
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return (int(trace_id[:8], 16) / 0xFFFFFFFF) < rate


class Client:
    def __init__(
        self, config: Config | None = None, *, transport: Any = None, **overrides: object
    ) -> None:
        self.config = config or Config.from_env(**overrides)
        self.exporter = Exporter(self.config, transport=transport)
        self._atexit_registered = False

    # ------------------------------------------------------------------- tracing

    @never_raises(default=NOOP)
    def start_trace(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceRecorder | Any:
        if not self.config.records:
            return NOOP
        identifier = trace_id or new_trace_id()
        recorder = TraceRecorder(
            name,
            config=self.config,
            trace_id=identifier,
            parent_span_id=parent_span_id,
            sampled=sampled(identifier, self.config.sample_rate),
        )
        if metadata:
            recorder.set_metadata(**metadata)
        self._register_atexit()
        return recorder

    @never_raises(default=NOOP)
    def start_span(
        self,
        name: str,
        *,
        span_type: SpanType | str = SpanType.CUSTOM,
        tool_name: str | None = None,
        trace: TraceRecorder | None = None,
        parent: SpanRecorder | None = None,
    ) -> SpanRecorder | Any:
        if not self.config.records:
            return NOOP

        active_trace = trace or ctx.current_trace()
        if active_trace is None:
            # An orphan span is more confusing than a synthetic root, and losing it
            # entirely is worse than both. Create a trace so the span has somewhere
            # to live, then emit it when the span closes.
            #
            # The common cause is a raw ThreadPoolExecutor: Python does not copy
            # contextvars across threads, so the worker sees no active trace. Say so,
            # because the fix (`evalforge.propagate`) is not discoverable otherwise.
            log_once(
                "client.orphan_span",
                f"span {name!r} was created with no active trace and has been recorded "
                "as its own trace. If this is a thread, wrap the callable with "
                "evalforge.propagate() to keep it attached to its parent.",
            )
            active_trace = self.start_trace(name)
            if active_trace is NOOP:
                return NOOP
            ctx.set_trace(active_trace)

        active_parent = parent or ctx.current_span()
        recorder = SpanRecorder(
            name,
            trace=active_trace,
            span_type=SpanType(span_type),
            parent_span_id=active_parent.span_id if active_parent else None,
            tool_name=tool_name,
            depth=(active_parent.depth + 1) if active_parent else 0,
        )
        if not active_trace.register(recorder):
            return NOOP
        return recorder

    @contextlib.contextmanager
    def trace(self, name: str, **kwargs: Any) -> Iterator[Any]:
        recorder = self.start_trace(name, **kwargs)
        if recorder is NOOP:
            yield NOOP
            return

        token = ctx.set_trace(recorder)
        try:
            yield recorder
        except BaseException as exc:
            recorder.status = Status.ERROR
            recorder.set_metadata(error=f"{type(exc).__name__}: {exc}"[:500])
            raise
        finally:
            recorder.end()
            ctx.reset_trace(token)
            self.emit(recorder)

    @contextlib.contextmanager
    def span(self, name: str, **kwargs: Any) -> Iterator[Any]:
        had_trace = ctx.current_trace() is not None
        recorder = self.start_span(name, **kwargs)
        if recorder is NOOP:
            yield NOOP
            return

        token = ctx.set_span(recorder)
        try:
            yield recorder
        except BaseException as exc:
            # Record and re-raise, untouched. The traceback the user sees must be
            # exactly the one their code produced.
            if isinstance(exc, Exception):
                recorder.set_error(exc)
            else:
                recorder.status = Status.ERROR
            raise
        finally:
            recorder.end()
            ctx.reset_span(token)
            if not had_trace:
                self._close_implicit_trace()

    def _close_implicit_trace(self) -> None:
        """Emit a trace `start_span` created on the caller's behalf.

        Without this the span is recorded into a trace nobody ever ends, and the
        data vanishes silently — the exact failure mode the SDK promises to avoid.
        """
        implicit = ctx.current_trace()
        if implicit is None:
            return
        implicit.end()
        self.emit(implicit)
        ctx.set_trace(None)

    # -------------------------------------------------------------------- export

    @never_raises()
    def emit(self, recorder: TraceRecorder) -> None:
        """Finish a trace: snapshot it and hand it to the exporter."""
        if not self.config.records:
            return
        keep = recorder.sampled or (
            self.config.always_sample_on_error and recorder.status is Status.ERROR
        )
        if not keep:
            return
        self.exporter.submit(recorder.snapshot())

    @never_raises(default=False)
    def flush(self, timeout: float | None = None) -> bool:
        return bool(self.exporter.flush(timeout))

    @never_raises()
    def shutdown(self, timeout: float | None = None) -> None:
        self.exporter.shutdown(timeout)

    def _register_atexit(self) -> None:
        if self._atexit_registered:
            return
        self._atexit_registered = True
        atexit.register(self.shutdown, self.config.shutdown_timeout_s)
