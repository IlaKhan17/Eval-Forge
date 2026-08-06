"""Trace ingestion.

Three properties this service must hold, in order of importance:

1. **Idempotent.** Retries are safe by construction, via `ON CONFLICT` on the
   natural key `(project_id, trace_id, span_id, started_at)`. No dedup table, no
   exactly-once delivery requirement, no coordination — the SDK can retry a batch as
   often as it likes and the result is identical.

2. **Order-independent.** A child span routinely arrives before its parent, and a
   whole trace can arrive before the trace row that describes it. Both are normal,
   so ingestion never assumes arrival order and stubs a trace from whichever span
   turns up first.

3. **Partially acceptable.** One bad span does not reject the batch around it.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Table, case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge_api.api.schemas.ingest import (
    IngestBatch,
    IngestResult,
    RejectedItem,
    SpanIn,
    TraceIn,
)
from evalforge_api.db.base import uuid7
from evalforge_api.db.models.identity import Environment, Project
from evalforge_api.db.models.traces import (
    INLINE_PAYLOAD_LIMIT,
    PayloadObject,
    Span,
    SpanEvent,
    Trace,
)
from evalforge_api.services import redaction, storage
from evalforge_api.services.storage import ObjectStore

logger = logging.getLogger("evalforge.ingest")

MAX_SPAN_PAYLOAD_BYTES = 1024 * 1024

# Core Tables rather than ORM classes. The trace row has a column literally named
# `metadata`, which on a declarative class resolves to SQLAlchemy's own MetaData
# object; keying by column name sidesteps the collision entirely.
TRACES = cast("Table", Trace.__table__)
SPANS = cast("Table", Span.__table__)
SPAN_EVENTS = cast("Table", SpanEvent.__table__)
ENVIRONMENTS = cast("Table", Environment.__table__)


@dataclass(slots=True)
class _Rollup:
    """Per-trace totals accumulated from the spans in this batch."""

    span_count: int = 0
    total_tokens: int = 0
    total_cost: Decimal = field(default_factory=lambda: Decimal(0))
    error_count: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None


class IngestService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        project: Project,
        store: ObjectStore,
        payload_ttl_days: int = 14,
    ) -> None:
        self.session = session
        self.project = project
        self.store = store
        self.payload_ttl_days = payload_ttl_days
        self.result = IngestResult()

    async def ingest(self, batch: IngestBatch) -> IngestResult:
        environment_id = await self._resolve_environment(batch)

        rollups: dict[str, _Rollup] = defaultdict(_Rollup)
        span_rows: list[dict[str, Any]] = []
        event_rows: list[dict[str, Any]] = []

        for span in batch.spans:
            prepared = await self._prepare_span(span, rollups)
            if prepared is None:
                continue
            span_rows.append(prepared)
            event_rows.extend(self._prepare_events(span))

        # Spans first, then rollups recomputed *from the table*, then traces.
        #
        # The obvious design — accumulate counters with `count = count + excluded` —
        # is wrong under replay: re-sending a batch adds its spans a second time and
        # the trace reports twice as many as exist. Since retries are expected and
        # safe by design, a counter that breaks on retry breaks constantly.
        # Recomputing costs one grouped aggregate over an indexed range and is
        # correct for replays, partial batches, and out-of-order arrival alike.
        await self._upsert_spans(span_rows)
        await self._insert_events(event_rows)
        touched = {row["trace_id"] for row in span_rows} | {t.trace_id for t in batch.traces}
        computed = await self._recompute_rollups(touched)
        await self._upsert_traces(batch, computed, environment_id, touched)

        self.result.accepted_events = len(event_rows)
        return self.result

    # ------------------------------------------------------------------ resolving

    async def _resolve_environment(self, batch: IngestBatch) -> uuid.UUID | None:
        name = batch.resource.environment or next(
            (t.environment for t in batch.traces if t.environment), None
        )
        if not name:
            return None

        name = name[:50]
        existing = await self.session.execute(
            select(Environment.id).where(
                Environment.project_id == self.project.id, Environment.name == name
            )
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            return found

        # Auto-create, upsert-style. Requiring an environment to be provisioned before its first
        # trace would mean silently dropping data from a newly deployed service.
        #
        # `ON CONFLICT DO NOTHING` rather than an insert, because check-then-insert is a race and
        # this is the exact moment it loses: a newly deployed service's *first* burst arrives on
        # several connections at once, all of them find no environment, and all but one get a
        # unique-violation 500. Found by tests/load/loadgen.py at 8-way concurrency — 11 batches
        # lost on a cold project, and invisible in every sequential test.
        inserted = await self.session.execute(
            insert(ENVIRONMENTS)
            .values(id=uuid7(), project_id=self.project.id, name=name)
            .on_conflict_do_nothing(index_elements=["project_id", "name"])
            .returning(ENVIRONMENTS.c.id)
        )
        created = inserted.scalar_one_or_none()
        if created is not None:
            return uuid.UUID(str(created))

        # DO NOTHING returns no row when another connection won, so read theirs.
        theirs = (
            await self.session.execute(
                select(Environment.id).where(
                    Environment.project_id == self.project.id, Environment.name == name
                )
            )
        ).scalar_one()
        return uuid.UUID(str(theirs))

    # -------------------------------------------------------------------- spans

    async def _prepare_span(
        self, span: SpanIn, rollups: dict[str, _Rollup]
    ) -> dict[str, Any] | None:
        payloads: dict[str, Any] = {}
        for field_name, value in (
            ("input", span.input),
            ("output", span.output),
            ("args", span.tool_args),
        ):
            if value is None:
                payloads[f"{field_name}_inline"] = None
                payloads[f"{field_name}_ref"] = None
                continue

            # Server-side redaction is a backstop, not the primary control: the SDK
            # already redacted before export. This catches a misconfigured or
            # outdated client, and reports it so they can fix the instrumentation.
            cleaned, redacted, truncated = redaction.scrub(value)
            self.result.secrets_redacted += redacted

            if truncated:
                # Redaction caps individual fields before this point, so an enormous
                # payload usually arrives here already clipped. Report it: a silently
                # shortened payload is how someone debugs the wrong thing for an hour.
                self.result.rejected.append(
                    RejectedItem(
                        kind="span_payload",
                        identifier=f"{span.span_id}.{field_name}",
                        reason=(
                            f"{truncated} field(s) exceeded the per-field size limit "
                            "and were truncated to a preview"
                        ),
                    )
                )

            raw = storage.serialize(cleaned)
            if len(raw) > MAX_SPAN_PAYLOAD_BYTES:
                self.result.rejected.append(
                    RejectedItem(
                        kind="span_payload",
                        identifier=f"{span.span_id}.{field_name}",
                        reason=(
                            f"payload is {len(raw)} bytes, over the "
                            f"{MAX_SPAN_PAYLOAD_BYTES} byte limit; the span was stored "
                            "without it"
                        ),
                    )
                )
                payloads[f"{field_name}_inline"] = {"_dropped": "payload_too_large"}
                payloads[f"{field_name}_ref"] = None
            elif len(raw) > INLINE_PAYLOAD_LIMIT:
                stored = await self._offload(cleaned)
                if stored is None:
                    # Object storage is unreachable. The span is kept without its payload rather
                    # than the batch being rejected: the skeleton carries the timings, the tokens,
                    # the tool names, and the parent links, which is everything a trajectory policy
                    # and every operational metric need. Losing all of that because a bucket is down
                    # turns a degraded dependency into an outage.
                    #
                    # Reported per span, not swallowed. A payload that silently is not there is
                    # indistinguishable from a span that never had one, and someone will spend an
                    # afternoon on that difference.
                    self.result.rejected.append(
                        RejectedItem(
                            kind="payload",
                            identifier=f"{span.trace_id}:{span.span_id}:{field_name}",
                            reason=(
                                "object storage was unavailable; the span was stored without this "
                                "payload"
                            ),
                        )
                    )
                    payloads[f"{field_name}_inline"] = {"_dropped": "storage_unavailable"}
                    payloads[f"{field_name}_ref"] = None
                else:
                    payloads[f"{field_name}_inline"] = None
                    payloads[f"{field_name}_ref"] = stored
            else:
                payloads[f"{field_name}_inline"] = cleaned
                payloads[f"{field_name}_ref"] = None

        rollup = rollups[span.trace_id]
        rollup.span_count += 1
        tokens = span.tokens.total if span.tokens else 0
        rollup.total_tokens += tokens
        if span.cost:
            rollup.total_cost += span.cost
        if span.status == "error":
            rollup.error_count += 1
        rollup.earliest = min(rollup.earliest or span.started_at, span.started_at)
        if span.ended_at:
            rollup.latest = max(rollup.latest or span.ended_at, span.ended_at)

        return {
            "id": uuid.uuid4(),
            "project_id": self.project.id,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "name": span.name,
            "span_type": span.span_type,
            "status": span.status,
            "status_message": span.status_message,
            "started_at": span.started_at,
            "ended_at": span.ended_at,
            "duration_ms": span.duration_ms,
            "attributes": redaction.scrub(span.attributes)[0],
            "model": span.model,
            "provider": span.provider,
            "prompt_tokens": span.tokens.prompt if span.tokens else 0,
            "completion_tokens": span.tokens.completion if span.tokens else 0,
            "total_tokens": tokens,
            "cost": span.cost,
            "tool_name": span.tool_name,
            "error_type": span.error_type,
            "sequence_index": span.sequence_index,
            "redaction_count": span.redaction_count,
            **payloads,
        }

    async def _offload(self, payload: Any) -> uuid.UUID | None:
        """Store a large payload once, by content hash. `None` when storage is unreachable.

        Degrading rather than failing is a deliberate choice about what ingestion is for. A trace
        whose payloads are missing still answers "what did the agent do, in what order, how long
        did it take, and what did it cost" — which is what trajectory policies and every operational
        metric are built on. A trace that was never accepted answers nothing, and the SDK's
        buffer is bounded, so a rejected batch is data permanently gone.
        """
        try:
            stored = storage.store_payload(self.store, self.project.id, payload)
        except Exception:
            # Logged once per batch at most by the caller's rejection list; not re-raised.
            logger.warning(
                "object storage unavailable; storing spans without their large payloads",
                extra={"project_id": str(self.project.id)},
            )
            return None

        existing = await self.session.execute(
            select(PayloadObject).where(
                PayloadObject.project_id == self.project.id,
                PayloadObject.sha256 == stored.sha256,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            return row.id

        row = PayloadObject(
            project_id=self.project.id,
            sha256=stored.sha256,
            bucket=stored.bucket,
            object_key=stored.object_key,
            size_bytes=stored.size_bytes,
            encoding=stored.encoding,
            expires_at=datetime.now(UTC) + timedelta(days=self.payload_ttl_days),
        )
        self.session.add(row)
        await self.session.flush()
        self.result.offloaded_payloads += 1
        return row.id

    def _prepare_events(self, span: SpanIn) -> list[dict[str, Any]]:
        return [
            {
                "id": uuid.uuid4(),
                "project_id": self.project.id,
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "name": event.name,
                "timestamp": event.timestamp,
                "attributes": event.attributes,
            }
            for event in span.events
        ]

    # ------------------------------------------------------------------- writes

    async def _upsert_spans(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return

        before = await self._count_spans({(r["trace_id"], r["span_id"]) for r in rows})

        statement = insert(SPANS).values(rows)
        statement = statement.on_conflict_do_update(
            constraint="uq_spans_natural_key",
            # A re-sent span updates rather than duplicates. Later data wins, which
            # is what makes it safe to send a span twice: once when it starts (open)
            # and again when it ends.
            set_={
                "name": statement.excluded.name,
                "status": statement.excluded.status,
                "status_message": statement.excluded.status_message,
                "ended_at": statement.excluded.ended_at,
                "duration_ms": statement.excluded.duration_ms,
                "attributes": statement.excluded.attributes,
                "input_inline": statement.excluded.input_inline,
                "output_inline": statement.excluded.output_inline,
                "args_inline": statement.excluded.args_inline,
                "input_ref": statement.excluded.input_ref,
                "output_ref": statement.excluded.output_ref,
                "args_ref": statement.excluded.args_ref,
                "total_tokens": statement.excluded.total_tokens,
                "cost": statement.excluded.cost,
            },
        )
        await self.session.execute(statement)

        after = await self._count_spans({(r["trace_id"], r["span_id"]) for r in rows})
        self.result.accepted_spans = len(rows)
        self.result.duplicate_spans = max(0, len(rows) - (after - before))

    async def _count_spans(self, keys: set[tuple[str, str]]) -> int:
        if not keys:
            return 0
        from sqlalchemy import func, tuple_  # noqa: PLC0415

        result = await self.session.execute(
            select(func.count())
            .select_from(Span)
            .where(
                Span.project_id == self.project.id,
                tuple_(Span.trace_id, Span.span_id).in_(list(keys)),
            )
        )
        return int(result.scalar_one())

    async def _recompute_rollups(self, trace_ids: set[str]) -> dict[str, _Rollup]:
        """Derive each trace's totals from the spans actually stored."""
        if not trace_ids:
            return {}

        from sqlalchemy import case, func  # noqa: PLC0415

        result = await self.session.execute(
            select(
                Span.trace_id,
                func.count().label("span_count"),
                func.coalesce(func.sum(Span.total_tokens), 0).label("tokens"),
                func.coalesce(func.sum(Span.cost), 0).label("cost"),
                func.count(case((Span.status == "error", 1))).label("errors"),
                func.min(Span.started_at).label("earliest"),
                func.max(Span.ended_at).label("latest"),
            )
            .where(Span.project_id == self.project.id, Span.trace_id.in_(list(trace_ids)))
            .group_by(Span.trace_id)
        )

        return {
            row.trace_id: _Rollup(
                span_count=row.span_count,
                total_tokens=int(row.tokens),
                total_cost=Decimal(row.cost),
                error_count=row.errors,
                earliest=row.earliest,
                latest=row.latest,
            )
            for row in result.all()
        }

    async def _upsert_traces(
        self,
        batch: IngestBatch,
        rollups: dict[str, _Rollup],
        environment_id: uuid.UUID | None,
        touched: set[str],
    ) -> None:
        declared = {t.trace_id: t for t in batch.traces}

        declared_rows: list[dict[str, Any]] = []
        stub_rows: list[dict[str, Any]] = []
        for trace_id in touched | set(declared):
            declaration = declared.get(trace_id)
            rollup = rollups.get(trace_id, _Rollup())
            row = self._trace_row(trace_id, declaration, rollup, environment_id, batch)
            (declared_rows if declaration is not None else stub_rows).append(row)

        # Two statements, because a stub must never overwrite what a declaration said.
        #
        # A trace's spans routinely arrive in several batches — that is the normal
        # behaviour of OTLP's BatchSpanProcessor, and it happens with the SDK too when a
        # long trace flushes more than once. A later batch of child spans carries no trace
        # declaration, so it is stubbed; if that stub took part in the same upsert it would
        # reset `name` to "unknown" and blank the metadata, tags, and state that the first
        # batch established. One SET clause for both cases silently destroys data on the
        # second batch of every multi-batch trace.
        await self._upsert_declared_traces(declared_rows)
        await self._upsert_stub_traces(stub_rows)
        self.result.accepted_traces = len(declared_rows) + len(stub_rows)

    async def _upsert_declared_traces(self, rows: list[dict[str, Any]]) -> None:
        """Traces the client described. The declaration is authoritative."""
        if not rows:
            return
        statement = insert(TRACES).values(rows)
        statement = statement.on_conflict_do_update(
            constraint="uq_traces_project_trace",
            set_={
                "name": statement.excluded.name,
                "status": statement.excluded.status,
                "ended_at": statement.excluded.ended_at,
                "duration_ms": statement.excluded.duration_ms,
                "metadata": statement.excluded.metadata,
                "tags": statement.excluded.tags,
                "state": statement.excluded.state,
                "session_id": statement.excluded.session_id,
                "user_ref": statement.excluded.user_ref,
                # Absolute, because the values were recomputed from the spans table
                # rather than accumulated from this batch.
                "span_count": statement.excluded.span_count,
                "total_tokens": statement.excluded.total_tokens,
                "total_cost": statement.excluded.total_cost,
                "error_count": statement.excluded.error_count,
                "dropped_span_count": statement.excluded.dropped_span_count,
            },
        )
        await self.session.execute(statement)

    async def _upsert_stub_traces(self, rows: list[dict[str, Any]]) -> None:
        """Traces inferred from their spans, because no declaration has arrived.

        Only the facts the spans actually establish are updated. Descriptive fields are
        left alone, so a stub cannot undo a declaration from an earlier batch.
        """
        if not rows:
            return
        statement = insert(TRACES).values(rows)
        statement = statement.on_conflict_do_update(
            constraint="uq_traces_project_trace",
            set_={
                # A trace only ever grows later, and spans can arrive out of order, so the
                # end time is the greatest seen rather than the newest written.
                "ended_at": func.greatest(TRACES.c.ended_at, statement.excluded.ended_at),
                "duration_ms": func.greatest(TRACES.c.duration_ms, statement.excluded.duration_ms),
                # A stub may escalate a trace to `error` but never downgrade one. Losing an
                # error because a later batch of healthy spans arrived would hide exactly
                # the traces worth looking at.
                "status": case(
                    (statement.excluded.error_count > 0, "error"),
                    else_=TRACES.c.status,
                ),
                "span_count": statement.excluded.span_count,
                "total_tokens": statement.excluded.total_tokens,
                "total_cost": statement.excluded.total_cost,
                "error_count": statement.excluded.error_count,
                "dropped_span_count": func.greatest(
                    TRACES.c.dropped_span_count, statement.excluded.dropped_span_count
                ),
            },
        )
        await self.session.execute(statement)

    def _trace_row(
        self,
        trace_id: str,
        declaration: TraceIn | None,
        rollup: _Rollup,
        environment_id: uuid.UUID | None,
        batch: IngestBatch,
    ) -> dict[str, Any]:
        started = declaration.started_at if declaration else (rollup.earliest or datetime.now(UTC))
        ended = declaration.ended_at if declaration else rollup.latest
        duration = (
            declaration.duration_ms
            if declaration and declaration.duration_ms is not None
            else (int((ended - started).total_seconds() * 1000) if ended else None)
        )
        status = declaration.status if declaration else ("error" if rollup.error_count else "ok")

        return {
            "id": uuid.uuid4(),
            "project_id": self.project.id,
            "environment_id": environment_id,
            "trace_id": trace_id,
            "name": declaration.name if declaration else "unknown",
            "status": status,
            "started_at": started,
            "ended_at": ended,
            "duration_ms": duration,
            "span_count": rollup.span_count,
            "total_tokens": rollup.total_tokens,
            "total_cost": rollup.total_cost,
            "error_count": rollup.error_count,
            "dropped_span_count": (declaration.dropped_span_count if declaration else 0)
            or batch.dropped_span_count,
            "git_commit": (declaration.git_commit if declaration else None)
            or batch.resource.git_commit,
            "session_id": declaration.session_id if declaration else None,
            "user_ref": declaration.user_ref if declaration else None,
            "capture_mode": declaration.capture_mode if declaration else "redacted",
            "metadata": redaction.scrub(declaration.metadata)[0] if declaration else {},
            "tags": declaration.tags if declaration else {},
            "state": redaction.scrub(declaration.state)[0] if declaration else {},
        }

    async def _insert_events(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        # Events have no natural key of their own, so a replayed batch would
        # duplicate them. Delete-then-insert per span keeps replay idempotent.
        from sqlalchemy import delete, tuple_  # noqa: PLC0415

        keys = {(r["trace_id"], r["span_id"]) for r in rows}
        await self.session.execute(
            delete(SpanEvent).where(
                SpanEvent.project_id == self.project.id,
                tuple_(SpanEvent.trace_id, SpanEvent.span_id).in_(list(keys)),
            )
        )
        await self.session.execute(insert(SPAN_EVENTS).values(rows))
