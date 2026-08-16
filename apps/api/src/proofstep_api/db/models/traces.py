"""Trace storage: traces, spans, span events, and payload objects.

**Partitioning.** `traces`, `spans`, and `span_events` are declared
`PARTITION BY RANGE` on their time column from the very first migration. Doing it
now is nearly free; retrofitting it onto a live billion-row table is a maintenance
window. It also turns retention into `DROP PARTITION` — instant — instead of a bulk
`DELETE` that bloats the heap and leaves the index fragmented (ADR-005).

Two consequences follow, and both are deliberate:

1. **Composite primary keys.** Postgres requires every unique constraint on a
   partitioned table to contain the partition key, so the key is `(id, started_at)`
   rather than `id` alone. These tables therefore do not use `IdentifiedBase`.

2. **No foreign keys into partitioned tables.** Referencing a composite key from
   three child tables would spread the partition column everywhere for no benefit.
   Spans and events instead carry the logical identifiers (`trace_id`, `span_id`)
   and are indexed on them. Ingestion already cannot rely on referential integrity:
   a child span routinely arrives before its parent, so enforcing an FK would mean
   rejecting valid data or buffering it (docs/DATABASE_DESIGN.md §2.2).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from proofstep_api.db.base import Base, IdentifiedBase, TimestampMixin, uuid7

SPAN_TYPES = (
    "agent",
    "workflow",
    "llm",
    "tool",
    "retriever",
    "embedding",
    "guardrail",
    "evaluator",
    "custom",
)
STATUSES = ("ok", "error", "timeout", "unset")

# Payloads below this go inline as JSONB; larger ones are offloaded to object
# storage. Small traces then cost one query, while a 2 MB document context does not
# bloat the row, thrash TOAST, or destroy cache locality for everything around it.
INLINE_PAYLOAD_LIMIT = 32 * 1024


class Trace(Base, TimestampMixin):
    __tablename__ = "traces"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column()
    environment_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    trace_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="ok")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    # Maintained incrementally on ingest. Listing traces must never aggregate over
    # spans: that turns the most-hit endpoint in the product into a join over the
    # largest table.
    span_count: Mapped[int] = mapped_column(Integer, default=0)
    dropped_span_count: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    error_count: Mapped[int] = mapped_column(Integer, default=0)

    error_category: Mapped[str | None] = mapped_column(String(100), default=None)
    git_commit: Mapped[str | None] = mapped_column(String(64), default=None)
    session_id: Mapped[str | None] = mapped_column(String(100), default=None)
    user_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    capture_mode: Mapped[str] = mapped_column(String(20), default="redacted")

    trace_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    # Tags live here rather than in a side table. The design considered a separate
    # `trace_tags` table for indexed filtering, but a GIN index over JSONB serves the
    # same queries at MVP scale, and a child table cannot hold a foreign key to a
    # partitioned parent without dragging the partition column along with it.
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        PrimaryKeyConstraint("id", "started_at"),
        # The idempotency anchor: re-sending a trace updates rather than duplicates.
        UniqueConstraint("project_id", "trace_id", "started_at", name="uq_traces_project_trace"),
        CheckConstraint(f"status IN {STATUSES}", name="status_valid"),
        Index("ix_traces_project_started", "project_id", "started_at"),
        Index("ix_traces_project_name_started", "project_id", "name", "started_at"),
        Index(
            "ix_traces_project_errors",
            "project_id",
            "started_at",
            postgresql_where="status = 'error'",
        ),
        Index("ix_traces_project_commit", "project_id", "git_commit"),
        Index("ix_traces_metadata", "metadata", postgresql_using="gin"),
        Index("ix_traces_tags", "tags", postgresql_using="gin"),
        {"postgresql_partition_by": "RANGE (started_at)"},
    )


class Span(Base, TimestampMixin):
    __tablename__ = "spans"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column()

    trace_id: Mapped[str] = mapped_column(String(64))
    span_id: Mapped[str] = mapped_column(String(64))
    parent_span_id: Mapped[str | None] = mapped_column(String(64), default=None)

    name: Mapped[str] = mapped_column(String(200))
    span_type: Mapped[str] = mapped_column(String(20), default="custom")
    status: Mapped[str] = mapped_column(String(16), default="ok")
    status_message: Mapped[str | None] = mapped_column(Text, default=None)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    input_inline: Mapped[Any | None] = mapped_column(JSONB, default=None)
    output_inline: Mapped[Any | None] = mapped_column(JSONB, default=None)
    args_inline: Mapped[Any | None] = mapped_column(JSONB, default=None)
    input_ref: Mapped[uuid.UUID | None] = mapped_column(default=None)
    output_ref: Mapped[uuid.UUID | None] = mapped_column(default=None)
    args_ref: Mapped[uuid.UUID | None] = mapped_column(default=None)

    # Denormalized hot-path columns rather than JSONB keys: these are filtered and
    # aggregated constantly, and a JSONB extraction in a WHERE clause cannot use a
    # plain B-tree index.
    model: Mapped[str | None] = mapped_column(String(200), default=None)
    provider: Mapped[str | None] = mapped_column(String(50), default=None)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    tool_name: Mapped[str | None] = mapped_column(String(200), default=None)
    error_type: Mapped[str | None] = mapped_column(String(100), default=None)

    sequence_index: Mapped[int] = mapped_column(Integer, default=0)
    redaction_count: Mapped[int] = mapped_column(SmallInteger, default=0)

    __table_args__ = (
        PrimaryKeyConstraint("id", "started_at"),
        # Makes ingestion idempotent via ON CONFLICT DO UPDATE. No dedup table and
        # no exactly-once delivery required: retries are simply safe.
        UniqueConstraint(
            "project_id", "trace_id", "span_id", "started_at", name="uq_spans_natural_key"
        ),
        CheckConstraint(f"span_type IN {SPAN_TYPES}", name="span_type_valid"),
        CheckConstraint(f"status IN {STATUSES}", name="status_valid"),
        # The dominant query: every span of one trace, in order.
        Index("ix_spans_project_trace", "project_id", "trace_id", "started_at"),
        Index("ix_spans_project_type_started", "project_id", "span_type", "started_at"),
        Index(
            "ix_spans_project_tool",
            "project_id",
            "tool_name",
            "started_at",
            postgresql_where="tool_name IS NOT NULL",
        ),
        Index(
            "ix_spans_project_model",
            "project_id",
            "model",
            "started_at",
            postgresql_where="model IS NOT NULL",
        ),
        Index("ix_spans_attributes", "attributes", postgresql_using="gin"),
        {"postgresql_partition_by": "RANGE (started_at)"},
    )


class SpanEvent(Base):
    """Point-in-time occurrences inside a span: retries, guardrail triggers.

    A separate table rather than a JSONB array on the span, for two reasons: retries
    and guardrail triggers must be independently queryable by operational and
    trajectory evaluators, and an unbounded array inside a hot row forces a TOAST
    rewrite on every append.
    """

    __tablename__ = "span_events"

    id: Mapped[uuid.UUID] = mapped_column(default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column()
    trace_id: Mapped[str] = mapped_column(String(64))
    span_id: Mapped[str] = mapped_column(String(64))

    name: Mapped[str] = mapped_column(String(200))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        PrimaryKeyConstraint("id", "timestamp"),
        Index("ix_span_events_project_span", "project_id", "trace_id", "span_id", "timestamp"),
        Index("ix_span_events_project_name", "project_id", "name", "timestamp"),
        {"postgresql_partition_by": "RANGE (timestamp)"},
    )


class PayloadObject(IdentifiedBase, TimestampMixin):
    """A content-addressed pointer into object storage.

    Deliberately not partitioned: it is deduplicated by content, so its growth is
    driven by distinct payloads rather than by request volume, and retention here is
    driven by `expires_at` rather than by age of the owning trace.

    The `(project_id, sha256)` uniqueness is what makes deduplication real. A system
    prompt repeated across ten thousand spans is stored once, which is a large
    saving in practice rather than a micro-optimization.
    """

    __tablename__ = "payload_objects"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32))
    bucket: Mapped[str] = mapped_column(String(100))
    object_key: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(100), default="application/json")
    encoding: Mapped[str] = mapped_column(String(20), default="gzip")
    redaction_applied: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        UniqueConstraint("project_id", "sha256", name="uq_payload_objects_content"),
        Index("ix_payload_objects_expiry", "expires_at", postgresql_where="expires_at IS NOT NULL"),
    )
