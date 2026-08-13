"""Queue observability: is the background work actually happening?

The failure this answers is the quietest one in the system. Ingestion is synchronous and its
problems surface as HTTP errors, but everything downstream — online evaluation, review-queue
escalation, lease recovery, retention — runs in a worker, and when a worker stops the API keeps
returning 200 to everything. The dashboard just gets emptier.

So these endpoints report the three things that distinguish "nothing to do" from "nothing is
being done":

1. **Job queue depth**, separated into ready and scheduled, from Redis.
2. **Dead letters** — jobs that failed every retry — with counts by job and the age of the
   oldest unresolved one.
3. **Review-queue depth and the age of the oldest pending item**, per project.

Scoping, stated because it is a judgement call: the review-queue view is tenant-scoped like
everything else, while the job queue and dead letters are deployment-wide. A dead letter carries
a job name, an exception type, and a truncated message, with arguments filtered to an allow-list
of ids and limits (`worker.deadletter.SAFE_CONTEXT_KEYS`) — no tenant content, and the job names
are the same five for every installation. Any authenticated project reader may therefore see
them, which is what makes this usable by the person who actually notices something is wrong.

`resolve` requires a configuration scope rather than read: acknowledging a failure is an
operational decision, and a read-only credential should not be able to make a broken job look
handled.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from evalforge_api.api.dependencies import SessionDep, SettingsDep
from evalforge_api.api.routes.online import Configurer, Reader
from evalforge_api.db.models.identity import Project
from evalforge_api.errors import NotFoundError
from evalforge_api.services import budget
from evalforge_api.worker import deadletter, jobs

router = APIRouter(prefix="/v1/ops", tags=["operations"])


class DeadLetterOut(BaseModel):
    id: uuid.UUID
    job_name: str
    job_id: str | None
    attempts: int
    error_type: str
    error_message: str
    context: dict[str, Any]
    created_at: str
    resolved_at: str | None


class ResolveIn(BaseModel):
    #: Required, not optional. "Resolved" with no note is indistinguishable from "dismissed
    #: because it was noisy", and the difference matters the third time it happens.
    note: str = Field(min_length=1, max_length=2_000)


class BudgetOut(BaseModel):
    """Where this project stands against its monthly ceiling."""

    #: Null means unlimited — deliberately distinct from 0, which is a real setting for a project
    #: that should run only its free deterministic rules.
    monthly_limit: float | None
    spent: float
    remaining: float | None
    #: Null when unlimited. A ratio of 0 would read as "spending nothing", which is a different
    #: fact from "cannot be over budget".
    ratio: float | None
    exhausted: bool
    month_start: str
    #: Stated in the response because a limit whose scope is assumed is worse than one whose scope
    #: is written down.
    covers: str = "server-initiated spend (online evaluation) only"


class BudgetIn(BaseModel):
    #: Null clears the ceiling. Explicit rather than omitted-means-unchanged, because "unlimited" is
    #: a decision someone makes and should look like one in the request.
    monthly_limit: float | None = Field(default=None, ge=0)


@router.get("/budget", response_model=BudgetOut, summary="Monthly spend against the ceiling")
async def read_budget(session: SessionDep, principal: Reader) -> BudgetOut:
    status = await budget.status(session, project_id=principal.project)
    return BudgetOut(
        monthly_limit=float(status.limit) if status.limit is not None else None,
        spent=float(status.spent),
        remaining=float(status.remaining) if status.remaining is not None else None,
        ratio=status.ratio,
        exhausted=status.exhausted,
        month_start=status.month_start.isoformat(),
    )


@router.put("/budget", response_model=BudgetOut, summary="Set the monthly ceiling")
async def set_budget(body: BudgetIn, session: SessionDep, principal: Configurer) -> BudgetOut:
    """Set or clear this project's monthly spend ceiling.

    A configuration scope, not read: raising a ceiling is how a bill gets bigger, and a credential
    that can only read traces should not be able to authorise that.
    """
    project = await session.get(Project, principal.project)
    if project is None:
        raise NotFoundError("No such project.")
    project.monthly_cost_limit = (
        None if body.monthly_limit is None else Decimal(str(body.monthly_limit))
    )
    await session.flush()
    return await read_budget(session, principal)


@router.get("/queues", summary="Job queue depth, dead letters, and review-queue health")
async def queues(
    session: SessionDep,
    settings: SettingsDep,
    principal: Reader,
    window_hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> dict[str, Any]:
    snapshot = await deadletter.queue_snapshot(settings.redis_url)
    summary = await deadletter.summary(session, window_hours=window_hours)
    review = await jobs.queue_health(session, project_id=principal.project)

    # `healthy` is a derived convenience, and deliberately conservative: an unreachable Redis
    # counts as unhealthy rather than unknown, because a queue nobody can see is a queue nobody
    # is draining. Callers that want the distinction have the fields to make it.
    healthy = snapshot.error is None and summary["unresolved"] == 0
    return {
        "healthy": healthy,
        "job_queue": snapshot.as_dict(),
        "dead_letters": summary,
        "review_queues": review,
    }


@router.get("/dead-letters", response_model=list[DeadLetterOut], summary="Unresolved job failures")
async def dead_letters(
    session: SessionDep,
    principal: Reader,  # noqa: ARG001 — authorisation guard; these records are not tenant-scoped
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeadLetterOut]:
    rows = await deadletter.unresolved(session, limit=limit)
    return [
        DeadLetterOut(
            id=row.id,
            job_name=row.job_name,
            job_id=row.job_id,
            attempts=row.attempts,
            error_type=row.error_type,
            error_message=row.error_message,
            context=row.context,
            created_at=row.created_at.isoformat(),
            resolved_at=row.resolved_at.isoformat() if row.resolved_at else None,
        )
        for row in rows
    ]


@router.post("/dead-letters/{dead_letter_id}/resolve", summary="Acknowledge a job failure")
async def resolve_dead_letter(
    dead_letter_id: uuid.UUID,
    body: ResolveIn,
    session: SessionDep,
    principal: Configurer,  # noqa: ARG001 — authorisation guard
) -> dict[str, Any]:
    resolved = await deadletter.resolve(session, dead_letter_id, note=body.note)
    if not resolved:
        # One message for both "no such record" and "already resolved". The second is the
        # common case — two people looking at the same incident — and it is not an error worth
        # a distinct code.
        raise NotFoundError("No unresolved dead letter with that id.")
    return {"id": str(dead_letter_id), "resolved": True}
