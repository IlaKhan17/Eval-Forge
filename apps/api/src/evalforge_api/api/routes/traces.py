"""Trace read APIs: list, detail, span tree, payload access.

Pagination is keyset, never OFFSET. `OFFSET 10000` on the span table is a full scan,
and the trace list is the most-hit endpoint in the product — the one place where the
wrong pagination strategy is guaranteed to hurt.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, and_, or_, select

from evalforge_api.api.dependencies import SessionDep, SettingsDep, get_principal
from evalforge_api.db.models.online import OnlineEvalRule, OnlineEvaluation
from evalforge_api.db.models.traces import PayloadObject, Span, SpanEvent, Trace
from evalforge_api.errors import BadRequestError, ForbiddenError, NotFoundError
from evalforge_api.security import cursors
from evalforge_api.security.permissions import Permission, Principal
from evalforge_api.services.storage import get_store

router = APIRouter(prefix="/v1/traces", tags=["traces"])

MAX_LIMIT = 200


class TraceSummary(BaseModel):
    trace_id: str
    name: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    span_count: int
    error_count: int
    total_tokens: int
    total_cost: float
    dropped_span_count: int
    git_commit: str | None
    metadata: dict[str, Any]
    tags: dict[str, Any]


class TracePage(BaseModel):
    data: list[TraceSummary]
    next_cursor: str | None = None
    has_more: bool = False


class SpanOut(BaseModel):
    span_id: str
    parent_span_id: str | None
    name: str
    span_type: str
    status: str
    status_message: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    attributes: dict[str, Any]
    input: Any = None
    output: Any = None
    tool_args: Any = None
    input_truncated: bool = False
    output_truncated: bool = False
    model: str | None
    provider: str | None
    total_tokens: int
    cost: float | None
    tool_name: str | None
    error_type: str | None
    sequence_index: int
    events: list[dict[str, Any]] = Field(default_factory=list)


class EvaluationOut(BaseModel):
    """What an online rule concluded about this trace."""

    rule_slug: str
    rule_kind: str
    verdict: str
    score: float | None
    #: Why this trace was evaluated at all — sampled, escalated, forced, or deterministic. Without
    #: it a reader cannot tell "checked and passed" from "not selected", which is the difference
    #: between coverage and a coverage gap.
    decision_reason: str
    error: str | None
    #: The failures, inconclusive rules, and warnings the evaluator produced. Exposed rather than
    #: summarised into the verdict: "which rule, in which span" is the whole value of a trajectory
    #: failure, and a verdict alone sends the reader back to guessing.
    detail: dict[str, Any]
    created_at: datetime


class TraceDetail(TraceSummary):
    state: dict[str, Any]
    spans: list[SpanOut]
    orphan_span_ids: list[str] = Field(default_factory=list)
    #: Online evaluations of this trace, newest first.
    #:
    #: On the trace rather than only in a review queue, because a queue holds the failures somebody
    #: escalated. A rule can run on every trace and escalate none of them — that is the normal
    #: configuration for a free deterministic policy — and without this the verdict exists in the
    #: database and nowhere a reader can see it.
    evaluations: list[EvaluationOut] = Field(default_factory=list)


async def require_read(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    if not principal.can(Permission.PROJECT_READ):
        raise ForbiddenError("This credential cannot read traces; it needs the 'read' scope.")
    if principal.project_id is None:
        raise ForbiddenError("Reading traces requires a project-scoped credential.")
    return principal


ReaderDep = Annotated[Principal, Depends(require_read)]


def _apply_filters(
    statement: Select[Any],
    *,
    name: str | None,
    status: str | None,
    git_commit: str | None,
    since: datetime | None,
    until: datetime | None,
    min_duration_ms: int | None,
    max_duration_ms: int | None,
    has_errors: bool | None,
) -> Select[Any]:
    if name:
        statement = statement.where(Trace.name == name)
    if status:
        statement = statement.where(Trace.status == status)
    if git_commit:
        statement = statement.where(Trace.git_commit == git_commit)
    if since:
        statement = statement.where(Trace.started_at >= since)
    if until:
        statement = statement.where(Trace.started_at < until)
    if min_duration_ms is not None:
        statement = statement.where(Trace.duration_ms >= min_duration_ms)
    if max_duration_ms is not None:
        statement = statement.where(Trace.duration_ms <= max_duration_ms)
    if has_errors is not None:
        statement = statement.where(Trace.error_count > 0 if has_errors else Trace.error_count == 0)
    return statement


@router.get("", response_model=TracePage, summary="List traces")
async def list_traces(  # noqa: PLR0917 — FastAPI declares query params as arguments
    session: SessionDep,
    settings: SettingsDep,
    principal: ReaderDep,
    name: str | None = None,
    status: str | None = None,
    git_commit: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    min_duration_ms: int | None = None,
    max_duration_ms: int | None = None,
    has_errors: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: str | None = None,
) -> TracePage:
    statement = select(Trace).where(Trace.project_id == principal.project_id)
    statement = _apply_filters(
        statement,
        name=name,
        status=status,
        git_commit=git_commit,
        since=since,
        until=until,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
        has_errors=has_errors,
    )

    if cursor:
        try:
            decoded = cursors.decode(cursor, secret=settings.jwt_secret)
        except cursors.InvalidCursorError as exc:
            # A tampered or stale cursor is a bad request, not a server fault. It
            # must not surface as a 500, both because that is wrong and because a
            # 500 invites retrying something that will never succeed.
            raise BadRequestError(
                "The pagination cursor is not valid. Start from the first page."
            ) from exc
        # Compound comparison on (started_at, id): ties on the timestamp are common
        # at high ingest rates, and comparing on time alone would silently skip or
        # repeat rows at a page boundary.
        anchor_time = datetime.fromisoformat(decoded["started_at"])
        anchor_id = uuid.UUID(decoded["id"])
        statement = statement.where(
            or_(
                Trace.started_at < anchor_time,
                and_(Trace.started_at == anchor_time, Trace.id < anchor_id),
            )
        )

    statement = statement.order_by(Trace.started_at.desc(), Trace.id.desc()).limit(limit + 1)
    rows = list((await session.execute(statement)).scalars().all())

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = cursors.encode(
            {"started_at": last.started_at.isoformat(), "id": str(last.id)},
            secret=settings.jwt_secret,
        )

    return TracePage(
        data=[_summary(row) for row in page], next_cursor=next_cursor, has_more=has_more
    )


@router.get("/{trace_id}", response_model=TraceDetail, summary="One trace with its span tree")
async def get_trace(
    trace_id: str, session: SessionDep, settings: SettingsDep, principal: ReaderDep
) -> TraceDetail:
    trace = (
        await session.execute(
            select(Trace).where(
                Trace.project_id == principal.project_id, Trace.trace_id == trace_id
            )
        )
    ).scalar_one_or_none()
    if trace is None:
        raise NotFoundError("No such trace.")

    spans = list(
        (
            await session.execute(
                select(Span)
                .where(Span.project_id == principal.project_id, Span.trace_id == trace_id)
                .order_by(Span.started_at, Span.sequence_index)
            )
        )
        .scalars()
        .all()
    )

    events = list(
        (
            await session.execute(
                select(SpanEvent)
                .where(
                    SpanEvent.project_id == principal.project_id,
                    SpanEvent.trace_id == trace_id,
                )
                .order_by(SpanEvent.timestamp)
            )
        )
        .scalars()
        .all()
    )
    by_span: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_span.setdefault(event.span_id, []).append(
            {
                "name": event.name,
                "timestamp": event.timestamp.isoformat(),
                "attributes": event.attributes,
            }
        )

    store = get_store(settings)
    refs = await _load_refs(session, spans, principal.project_id)

    known = {span.span_id for span in spans}
    orphans = [
        span.span_id
        for span in spans
        if span.parent_span_id is not None and span.parent_span_id not in known
    ]

    detail = _summary(trace).model_dump()
    detail["state"] = trace.state
    detail["spans"] = [
        _span_out(span, by_span.get(span.span_id, []), refs, store) for span in spans
    ]
    # Reported rather than hidden: an orphan usually means spans were dropped, and
    # the trajectory engine treats an incomplete trace differently for good reason.
    detail["orphan_span_ids"] = orphans
    detail["evaluations"] = await _load_evaluations(session, trace_id, principal.project_id)
    return TraceDetail.model_validate(detail)


async def _load_evaluations(
    session: SessionDep, trace_id: str, project_id: uuid.UUID | None
) -> list[dict[str, Any]]:
    """Online evaluations for one trace, joined to the rule that produced them.

    Joined rather than returning a rule id: an id sends the reader on a second request to learn what
    checked their trace, and the slug is what the rule is called everywhere else.
    """
    rows = (
        await session.execute(
            select(OnlineEvaluation, OnlineEvalRule)
            .join(OnlineEvalRule, OnlineEvalRule.id == OnlineEvaluation.rule_id)
            .where(
                OnlineEvaluation.project_id == project_id,
                OnlineEvaluation.trace_id == trace_id,
            )
            .order_by(OnlineEvaluation.created_at.desc())
        )
    ).all()
    return [
        {
            "rule_slug": rule.slug,
            "rule_kind": rule.kind,
            "verdict": evaluation.verdict,
            "score": evaluation.score,
            "decision_reason": evaluation.decision_reason,
            "error": evaluation.error,
            "detail": evaluation.detail or {},
            "created_at": evaluation.created_at,
        }
        for evaluation, rule in rows
    ]


async def _load_refs(
    session: SessionDep, spans: list[Span], project_id: uuid.UUID | None
) -> dict[uuid.UUID, PayloadObject]:
    ids = {
        ref
        for span in spans
        for ref in (span.input_ref, span.output_ref, span.args_ref)
        if ref is not None
    }
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(PayloadObject).where(
                PayloadObject.project_id == project_id, PayloadObject.id.in_(ids)
            )
        )
    ).scalars()
    return {row.id: row for row in rows}


def _summary(trace: Trace) -> TraceSummary:
    return TraceSummary(
        trace_id=trace.trace_id,
        name=trace.name,
        status=trace.status,
        started_at=trace.started_at,
        ended_at=trace.ended_at,
        duration_ms=trace.duration_ms,
        span_count=trace.span_count,
        error_count=trace.error_count,
        total_tokens=trace.total_tokens,
        total_cost=float(trace.total_cost),
        dropped_span_count=trace.dropped_span_count,
        git_commit=trace.git_commit,
        metadata=trace.trace_metadata,
        tags=trace.tags,
    )


def _span_out(
    span: Span,
    events: list[dict[str, Any]],
    refs: dict[uuid.UUID, PayloadObject],
    store: Any,
) -> SpanOut:
    def resolve(inline: Any, ref: uuid.UUID | None) -> tuple[Any, bool]:
        if ref is None:
            return inline, False
        record = refs.get(ref)
        if record is None:
            return None, True
        try:
            from evalforge_api.services.storage import load_payload  # noqa: PLC0415

            return load_payload(store, record.object_key), False
        except (KeyError, OSError, ValueError):
            # The row survives its object: retention removed the payload, or the
            # bucket is unreachable. Say so rather than pretending the span had no
            # input at all.
            return None, True

    input_value, input_missing = resolve(span.input_inline, span.input_ref)
    output_value, output_missing = resolve(span.output_inline, span.output_ref)
    args_value, _ = resolve(span.args_inline, span.args_ref)

    return SpanOut(
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        name=span.name,
        span_type=span.span_type,
        status=span.status,
        status_message=span.status_message,
        started_at=span.started_at,
        ended_at=span.ended_at,
        duration_ms=span.duration_ms,
        attributes=span.attributes,
        input=input_value,
        output=output_value,
        tool_args=args_value,
        input_truncated=input_missing,
        output_truncated=output_missing,
        model=span.model,
        provider=span.provider,
        total_tokens=span.total_tokens,
        cost=float(span.cost) if span.cost is not None else None,
        tool_name=span.tool_name,
        error_type=span.error_type,
        sequence_index=span.sequence_index,
        events=events,
    )
