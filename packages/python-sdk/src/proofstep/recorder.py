"""Mutable span and trace recorders that produce the frozen shared types.

The recorder is what user code touches; `Span`/`Trace` from `proofstep-types` are
the immutable snapshots it emits. Keeping those two apart is what lets the wire
format stay frozen and validated while the in-flight object stays cheap to mutate.
"""

from __future__ import annotations

import secrets
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from proofstep.safety import log_once, never_raises
from proofstep_core.redaction import RedactionPipeline
from proofstep_types import CaptureMode, Span, SpanEvent, SpanType, Status, TokenUsage, Trace

if TYPE_CHECKING:
    from proofstep.config import Config


def new_trace_id() -> str:
    return secrets.token_hex(16)  # 32 hex chars, W3C-compatible


def new_span_id() -> str:
    return secrets.token_hex(8)  # 16 hex chars


def _now() -> datetime:
    return datetime.now(UTC)


class SpanRecorder:
    """One operation in progress."""

    __slots__ = (
        "_ended",
        "_trace",
        "args",
        "attributes",
        "cost",
        "depth",
        "ended_at",
        "error_type",
        "events",
        "input",
        "model",
        "name",
        "output",
        "parent_span_id",
        "provider",
        "sequence_index",
        "span_id",
        "span_type",
        "started_at",
        "status",
        "status_message",
        "tokens",
        "tool_name",
        "trace_id",
    )

    def __init__(
        self,
        name: str,
        *,
        trace: TraceRecorder,
        span_type: SpanType = SpanType.CUSTOM,
        parent_span_id: str | None = None,
        span_id: str | None = None,
        tool_name: str | None = None,
        depth: int = 0,
    ) -> None:
        self._trace = trace
        self._ended = False
        self.span_id = span_id or new_span_id()
        self.trace_id = trace.trace_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.span_type = span_type
        self.tool_name = tool_name
        self.depth = depth
        self.started_at = _now()
        self.ended_at: datetime | None = None
        self.status = Status.OK
        self.status_message: str | None = None
        self.attributes: dict[str, Any] = {}
        self.input: Any = None
        self.output: Any = None
        self.args: dict[str, Any] | None = None
        self.events: list[SpanEvent] = []
        self.model: str | None = None
        self.provider: str | None = None
        self.tokens: TokenUsage | None = None
        self.cost: Decimal | None = None
        self.error_type: str | None = None
        self.sequence_index = trace.next_sequence()

    # ------------------------------------------------------------------- user API

    @never_raises()
    def set_input(self, value: Any) -> None:
        self.input = value

    @never_raises()
    def set_output(self, value: Any) -> None:
        self.output = value

    @never_raises()
    def set_args(self, value: dict[str, Any]) -> None:
        self.args = value

    @never_raises()
    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    @never_raises()
    def set_attributes(self, **values: Any) -> None:
        self.attributes.update(values)

    @never_raises()
    def set_model(
        self,
        model: str,
        *,
        provider: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: Decimal | float | None = None,
    ) -> None:
        self.model = model
        self.provider = provider
        self.tokens = TokenUsage(
            prompt=prompt_tokens,
            completion=completion_tokens,
            total=prompt_tokens + completion_tokens,
        )
        if cost is not None:
            # str() first: Decimal(float) inherits the float's imprecision, and cost
            # totals that drift are worse than no cost at all.
            self.cost = cost if isinstance(cost, Decimal) else Decimal(str(cost))

    @never_raises()
    def record_event(self, name: str, **attributes: Any) -> None:
        self.events.append(SpanEvent(name=name, timestamp=_now(), attributes=attributes))

    @never_raises()
    def set_error(self, exc: BaseException) -> None:
        self.status = Status.ERROR
        self.error_type = type(exc).__name__
        self.status_message = str(exc)[:1000]

    @never_raises()
    def end(self, *, status: Status | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        self.ended_at = _now()
        if status is not None:
            self.status = status
        self._trace.finish_span(self)

    # ---------------------------------------------------------------- conversion

    def snapshot(self, pipeline: RedactionPipeline) -> Span:
        redactions = 0
        payloads: dict[str, Any] = {}

        for field in ("input", "output", "args"):
            value = getattr(self, field)
            if value is None:
                continue
            payloads[field] = pipeline.apply(value, path=field)
            redactions += pipeline.count

        attributes = pipeline.apply(self.attributes, path="attributes") or {}
        redactions += pipeline.count

        return Span(
            span_id=self.span_id,
            trace_id=self.trace_id,
            parent_span_id=self.parent_span_id,
            name=self.name,
            span_type=self.span_type,
            status=self.status,
            status_message=self.status_message,
            started_at=self.started_at,
            ended_at=self.ended_at,
            attributes=attributes,
            input=payloads.get("input"),
            output=payloads.get("output"),
            events=self.events,
            model=self.model,
            provider=self.provider,
            tokens=self.tokens,
            cost=self.cost,
            tool_name=self.tool_name,
            tool_args=payloads.get("args"),
            error_type=self.error_type,
            sequence_index=self.sequence_index,
            redaction_count=redactions,
        )

    def __repr__(self) -> str:
        return f"<Span {self.name!r} {self.span_id} {self.status.value}>"


class TraceRecorder:
    """One workflow execution in progress."""

    def __init__(
        self,
        name: str,
        *,
        config: Config,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        sampled: bool = True,
    ) -> None:
        self.trace_id = trace_id or new_trace_id()
        self.name = name
        self.config = config
        self.sampled = sampled
        self.root_parent_span_id = parent_span_id
        self.started_at = _now()
        self.ended_at: datetime | None = None
        self.status = Status.OK
        self.metadata: dict[str, Any] = {}
        self.tags: dict[str, str] = {}
        self.state: dict[str, Any] = {}
        self.dropped_span_count = 0

        self._spans: list[SpanRecorder] = []
        self._open = 0
        self._sequence = 0
        self._lock = threading.Lock()

    def next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def register(self, span: SpanRecorder) -> bool:
        """Accept a span unless the trace is already at its cap."""
        with self._lock:
            if len(self._spans) >= self.config.max_spans_per_trace:
                self.dropped_span_count += 1
                log_once(
                    "trace.span_cap",
                    f"trace {self.name!r} hit max_spans_per_trace "
                    f"({self.config.max_spans_per_trace}); further spans are dropped "
                    "and counted in dropped_span_count",
                )
                return False
            self._spans.append(span)
            self._open += 1
            return True

    def finish_span(self, _span: SpanRecorder) -> None:
        with self._lock:
            self._open = max(0, self._open - 1)

    @never_raises()
    def set_metadata(self, **values: Any) -> None:
        self.metadata.update(values)

    @never_raises()
    def set_tags(self, **values: str) -> None:
        self.tags.update({k: str(v) for k, v in values.items()})

    @never_raises()
    def set_state(self, **values: Any) -> None:
        """Explicit workflow state.

        Exists so `final_state` and `conditional` policy rules have a defined data
        source instead of scraping outputs for something that looks like a status.
        """
        self.state.update(values)

    @never_raises()
    def end(self, *, status: Status | None = None) -> None:
        if self.ended_at is not None:
            return
        self.ended_at = _now()
        if status is not None:
            self.status = status
        elif any(s.status is Status.ERROR for s in self._spans):
            self.status = Status.ERROR

    @property
    def open_span_count(self) -> int:
        return self._open

    def snapshot(self) -> Trace:
        """Freeze into the shared `Trace` type, applying redaction."""
        pipeline = RedactionPipeline(
            redactors=[*_configured_redactors(self.config)],
            capture_mode=self.config.capture_mode,
            max_field_bytes=self.config.max_field_bytes,
        )
        spans = [s.snapshot(pipeline) for s in list(self._spans)]

        # Trace-level fields go through the same pipeline as span payloads. They are
        # user-supplied values like any other, and a credential passed to
        # `set_metadata` is exactly as much of a leak as one in a span input.
        #
        # `metadata_only` capture is the exception: it suppresses payloads, but
        # metadata, tags and state *are* the metadata, so they survive with secrets
        # still stripped.
        keep_structure = pipeline.capture_mode is CaptureMode.METADATA_ONLY
        scrub = _MetadataScrubber(self.config, keep_structure=keep_structure)

        return Trace(
            trace_id=self.trace_id,
            name=self.name,
            status=self.status,
            started_at=self.started_at,
            ended_at=self.ended_at,
            spans=spans,
            metadata=scrub(self.metadata, "metadata"),
            tags={k: str(v) for k, v in scrub(self.tags, "tags").items()},
            state=scrub(self.state, "state"),
            environment=self.config.environment,
            git_commit=self.config.git_commit,
            dropped_span_count=self.dropped_span_count,
        )

    def __repr__(self) -> str:
        return f"<Trace {self.name!r} {self.trace_id} spans={len(self._spans)}>"


def _configured_redactors(config: Config) -> list[Any]:
    from proofstep import redaction  # noqa: PLC0415 — breaks an import cycle

    # Always includes the secret redactors. `full` capture means full *payloads*,
    # never full credentials, and returning an empty list here previously left
    # trace-level metadata unredacted in that mode.
    extra = list(redaction.default())
    if config.redact_keys:
        extra.append(redaction.keys(config.redact_keys))
    return extra


class _MetadataScrubber:
    """Redacts trace-level dictionaries.

    Uses a pipeline pinned to REDACTED rather than the project's capture mode, so
    that `metadata_only` still keeps its metadata while `disabled` drops everything.
    """

    def __init__(self, config: Config, *, keep_structure: bool) -> None:
        mode = CaptureMode.REDACTED if keep_structure else config.capture_mode
        self._pipeline = RedactionPipeline(
            redactors=[*_configured_redactors(config)],
            capture_mode=CaptureMode.REDACTED if mode.stores_payloads or keep_structure else mode,
            max_field_bytes=config.max_field_bytes,
        )

    def __call__(self, value: dict[str, Any], path: str) -> dict[str, Any]:
        if not value:
            return {}
        result = self._pipeline.apply(value, path=path)
        return result if isinstance(result, dict) else {}
