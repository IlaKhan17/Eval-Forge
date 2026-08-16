"""Ingestion wire models.

Deliberately absent: any tenant identifier. Project and environment come from the
authenticated API key, never from the body. A client-supplied `project_id` that
reaches a query is one of the most common multi-tenant breaches, and the cleanest
defence is for the field not to exist.

Every limit here is enforced by the schema rather than by a handler, so a malformed
or hostile batch is rejected during parsing instead of part-way through a write.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SPANS_PER_BATCH = 10_000
MAX_TRACES_PER_BATCH = 1_000
MAX_EVENTS_PER_SPAN = 500
ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"


class SpanEventIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(max_length=200)
    timestamp: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class TokenUsageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt: int = Field(default=0, ge=0)
    completion: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)


class SpanIn(BaseModel):
    # `ignore`, not `forbid`: a newer SDK sending a field this server has not learned
    # yet must not have its whole batch rejected. Forward compatibility matters more
    # here than strictness, because SDK and server versions drift in the field.
    model_config = ConfigDict(extra="ignore")

    trace_id: str = Field(pattern=ID_PATTERN)
    span_id: str = Field(pattern=ID_PATTERN)
    parent_span_id: str | None = Field(default=None, pattern=ID_PATTERN)

    name: str = Field(max_length=200)
    span_type: str = "custom"
    status: str = "ok"
    status_message: str | None = Field(default=None, max_length=4000)

    started_at: datetime
    ended_at: datetime | None = None

    attributes: dict[str, Any] = Field(default_factory=dict)
    input: Any = None
    output: Any = None
    tool_args: dict[str, Any] | None = None
    events: list[SpanEventIn] = Field(default_factory=list, max_length=MAX_EVENTS_PER_SPAN)

    model: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(default=None, max_length=50)
    tokens: TokenUsageIn | None = None
    cost: Decimal | None = None
    tool_name: str | None = Field(default=None, max_length=200)
    error_type: str | None = Field(default=None, max_length=100)
    sequence_index: int = 0
    redaction_count: int = 0

    @field_validator("span_type")
    @classmethod
    def _known_span_type(cls, value: str) -> str:
        from proofstep_api.db.models.traces import SPAN_TYPES  # noqa: PLC0415 — avoids a cycle

        # Degrade rather than reject. A span type this server does not recognise is
        # still a span worth keeping, and a CHECK-constraint violation mid-batch
        # would lose the valid spans around it.
        return value if value in SPAN_TYPES else "custom"

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        from proofstep_api.db.models.traces import STATUSES  # noqa: PLC0415

        return value if value in STATUSES else "unset"

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return max(0, int((self.ended_at - self.started_at).total_seconds() * 1000))


class TraceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trace_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(max_length=200)
    status: str = "ok"
    started_at: datetime
    ended_at: datetime | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)
    tags: dict[str, Any] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)

    environment: str | None = Field(default=None, max_length=50)
    git_commit: str | None = Field(default=None, max_length=64)
    session_id: str | None = Field(default=None, max_length=100)
    user_ref: str | None = Field(default=None, max_length=200)
    capture_mode: str = "redacted"
    dropped_span_count: int = 0

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        from proofstep_api.db.models.traces import STATUSES  # noqa: PLC0415

        return value if value in STATUSES else "unset"

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return max(0, int((self.ended_at - self.started_at).total_seconds() * 1000))


class ResourceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    service_name: str | None = Field(default=None, alias="service.name", max_length=200)
    environment: str | None = Field(default=None, max_length=50)
    git_commit: str | None = Field(default=None, alias="git.commit", max_length=64)
    sdk_name: str | None = Field(default=None, alias="sdk.name", max_length=100)
    sdk_version: str | None = Field(default=None, alias="sdk.version", max_length=50)


class IngestBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resource: ResourceIn = Field(default_factory=ResourceIn)
    traces: list[TraceIn] = Field(default_factory=list, max_length=MAX_TRACES_PER_BATCH)
    spans: list[SpanIn] = Field(default_factory=list, max_length=MAX_SPANS_PER_BATCH)
    dropped_span_count: int = 0


class RejectedItem(BaseModel):
    kind: str
    identifier: str
    reason: str


class IngestResult(BaseModel):
    """Partial acceptance is the point.

    One oversized span must not poison the whole trace: the valid spans around it
    are stored and the rejection is reported per item, so a client can fix the one
    thing rather than guess which of five hundred spans was the problem.
    """

    accepted_traces: int = 0
    accepted_spans: int = 0
    accepted_events: int = 0
    duplicate_spans: int = 0
    offloaded_payloads: int = 0
    secrets_redacted: int = 0
    rejected: list[RejectedItem] = Field(default_factory=list)
