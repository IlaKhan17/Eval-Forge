"""Traces and spans — the captured record of one workflow execution."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from proofstep_types.common import SpanType, Status


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt: int = 0
    completion: int = 0
    total: int = 0


class SpanEvent(BaseModel):
    """A point-in-time occurrence inside a span: a retry, a guardrail trigger.

    Kept as a distinct type rather than a JSON array on the span because retries and
    guardrail triggers must be independently queryable for operational and trajectory
    evaluators (docs/DATABASE_DESIGN.md §2.2).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    timestamp: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class Span(BaseModel):
    """One operation inside a trace."""

    model_config = ConfigDict(frozen=True)

    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    name: str
    span_type: SpanType = SpanType.CUSTOM
    status: Status = Status.OK
    status_message: str | None = None
    started_at: datetime
    ended_at: datetime | None = None

    attributes: dict[str, Any] = Field(default_factory=dict)
    input: Any = None
    output: Any = None
    events: list[SpanEvent] = Field(default_factory=list)

    # Denormalized hot-path fields — these are real columns in the database rather
    # than JSONB keys because they are filtered and aggregated constantly.
    model: str | None = None
    provider: str | None = None
    tokens: TokenUsage | None = None
    cost: Decimal | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    error_type: str | None = None

    sequence_index: int = Field(
        default=0,
        description=(
            "Monotonic counter from the SDK. Breaks ordering ties when two spans "
            "share a start timestamp, which clock granularity makes common."
        ),
    )
    redaction_count: int = 0

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at).total_seconds() * 1000)

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


class Trace(BaseModel):
    """One complete AI workflow execution."""

    model_config = ConfigDict(frozen=True)

    trace_id: str
    name: str
    status: Status = Status.OK
    started_at: datetime
    ended_at: datetime | None = None
    spans: list[Span] = Field(default_factory=list)

    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)
    state: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Explicit workflow state set via `set_state`. Gives `final_state` and "
            "`conditional` policy rules a defined data source instead of scraping outputs."
        ),
    )

    environment: str | None = None
    git_commit: str | None = None
    dropped_span_count: int = Field(
        default=0,
        description=(
            "Spans the exporter dropped under backpressure. Non-zero makes the "
            "trajectory `incomplete`, which turns `required_*` policy rules into "
            "`inconclusive` rather than `fail` — asserting absence over incomplete "
            "data is unsound (docs/TRAJECTORY_POLICIES.md §4)."
        ),
    )

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at).total_seconds() * 1000)

    @property
    def is_complete(self) -> bool:
        return self.dropped_span_count == 0 and not any(s.is_open for s in self.spans)

    @property
    def total_cost(self) -> Decimal:
        return sum((s.cost for s in self.spans if s.cost is not None), Decimal(0))

    @property
    def total_tokens(self) -> int:
        return sum(s.tokens.total for s in self.spans if s.tokens is not None)

    def spans_by_type(self, *types: SpanType) -> list[Span]:
        wanted = set(types)
        return [s for s in self.spans if s.span_type in wanted]

    def find_span(self, span_id: str) -> Span | None:
        return next((s for s in self.spans if s.span_id == span_id), None)
