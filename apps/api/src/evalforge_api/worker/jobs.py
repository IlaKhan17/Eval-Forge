"""Background jobs: online evaluation, rollups, retention, lease recovery.

Every job here is written to the same three rules, because a background job that violates
any of them causes a problem nobody notices for weeks:

1. **Idempotent.** A job may run twice — a redeploy mid-execution, a retry after a
   transient database error, an operator running it by hand. Running twice must produce the
   same state as running once, or an online metric drifts upward on every replay.

2. **Bounded.** Every job takes a limit and processes at most that much. An unbounded job
   over a table that grows with traffic is a job that eventually holds a transaction open
   long enough to block ingestion.

3. **Per-project, in a loop.** One project's enormous backlog must not starve every other
   project. A single query across all projects would let the noisiest tenant monopolise the
   worker.

The jobs live inside `evalforge_api` rather than a separate distribution because they need
every model and service the API has. A separate package would duplicate the entire
dependency set and force the models into a third shared one for no gain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from evalforge_api.db.models.identity import Project
from evalforge_api.db.models.online import OnlineEvaluation, ReviewAssignment, ReviewQueue
from evalforge_api.services.online_eval import DEFAULT_BATCH_SIZE, OnlineEvalService
from evalforge_api.services.retention import RetentionService, drop_expired_partitions
from evalforge_api.services.review import ReviewService
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: Projects handled per invocation. Bounded for the same reason everything else is.
MAX_PROJECTS_PER_RUN = 100


@dataclass(slots=True)
class JobReport:
    """What a job did. Returned rather than only logged, so tests assert on behaviour."""

    job: str
    projects: int = 0
    processed: int = 0
    written: int = 0
    failures: int = 0
    errors: int = 0
    queued_for_review: int = 0
    released: int = 0
    cost: Decimal = Decimal(0)
    detail: dict[str, Any] = field(default_factory=dict)


async def active_project_ids(
    session: AsyncSession, *, limit: int = MAX_PROJECTS_PER_RUN
) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(Project.id)
                .where(Project.deleted_at.is_(None))
                .order_by(Project.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def run_online_eval(
    session: AsyncSession,
    *,
    project_ids: list[uuid.UUID] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    window_hours: int = 24,
    now: datetime | None = None,
) -> JobReport:
    """Evaluate traces that no rule has recorded a decision for yet.

    The window bounds how far back a restarted worker will reach. It is deliberately not
    unbounded: after an outage, replaying a month of traffic through judges is a bill nobody
    authorised, and the traces that matter most are the recent ones. Older gaps are
    recoverable by asking for them explicitly.
    """
    moment = now or datetime.now(UTC)
    since = moment - timedelta(hours=window_hours)
    report = JobReport(job="online_eval")

    for project_id in project_ids if project_ids is not None else await active_project_ids(session):
        service = OnlineEvalService(session, project_id=project_id)
        rules = await service.active_rules()
        if not rules:
            continue

        outcome = await service.run_batch(since=since, limit=batch_size, rules=rules, now=moment)
        report.projects += 1
        report.processed += outcome.traces_considered
        report.written += outcome.evaluations_written
        report.failures += outcome.failures
        report.errors += outcome.errors
        report.queued_for_review += outcome.queued_for_review
        report.cost += outcome.cost
        for reason, count in outcome.reasons.items():
            report.detail[reason] = report.detail.get(reason, 0) + count

    return report


async def release_expired_leases(
    session: AsyncSession,
    *,
    project_ids: list[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> JobReport:
    """Return abandoned review claims to their queues.

    `claim_next` already reclaims expired leases opportunistically, so this job exists for
    the case that path does not cover: a queue nobody is currently working. Without it, the
    last few items of an abandoned session sit in `in_review` forever and the queue looks
    busier than it is.
    """
    report = JobReport(job="release_expired_leases")
    for project_id in project_ids if project_ids is not None else await active_project_ids(session):
        released = await ReviewService(session, project_id=project_id).release_expired(now=now)
        report.released += released
        if released:
            report.projects += 1
    return report


async def rollup_online_metrics(
    session: AsyncSession,
    *,
    project_ids: list[uuid.UUID] | None = None,
    window_hours: int = 24,
    now: datetime | None = None,
) -> JobReport:
    """Summarise online evaluations per rule over a window.

    Computed on read rather than stored, for now. The honest reason: a stored rollup needs
    its own invalidation story, and getting that wrong produces numbers that are confidently
    stale — worse than numbers that take a moment to compute. When the read cost becomes real
    this becomes a materialised table, and the shape returned here is what it will hold.

    `skipped` is reported alongside `pass` and `fail` deliberately. A pass rate computed over
    evaluated traces alone answers a different question from one computed over all traces,
    and only reporting the numerator invites the wrong reading.
    """
    moment = now or datetime.now(UTC)
    since = moment - timedelta(hours=window_hours)
    report = JobReport(job="rollup_online_metrics")

    for project_id in project_ids if project_ids is not None else await active_project_ids(session):
        rows = (
            await session.execute(
                select(
                    OnlineEvaluation.rule_id,
                    OnlineEvaluation.verdict,
                    func.count(),
                    func.coalesce(func.sum(OnlineEvaluation.cost), 0),
                )
                .where(
                    OnlineEvaluation.project_id == project_id,
                    OnlineEvaluation.created_at >= since,
                )
                .group_by(OnlineEvaluation.rule_id, OnlineEvaluation.verdict)
            )
        ).all()
        if not rows:
            continue

        per_rule: dict[str, dict[str, Any]] = {}
        for rule_id, verdict, count, cost in rows:
            bucket = per_rule.setdefault(
                str(rule_id),
                {"pass": 0, "fail": 0, "inconclusive": 0, "error": 0, "skipped": 0, "cost": "0"},
            )
            bucket[str(verdict)] = int(count)
            bucket["cost"] = str(Decimal(bucket["cost"]) + Decimal(str(cost)))

        for stats in per_rule.values():
            evaluated = stats["pass"] + stats["fail"]
            # None, not 0.0, when nothing was evaluated. A pass rate of zero over zero
            # measurements would show as a total collapse on a dashboard.
            stats["pass_rate"] = (stats["pass"] / evaluated) if evaluated else None
            stats["evaluated"] = evaluated
            stats["coverage"] = (
                evaluated / (evaluated + stats["skipped"])
                if (evaluated + stats["skipped"])
                else None
            )

        report.projects += 1
        report.detail[str(project_id)] = per_rule

    return report


async def sweep_retention(
    session: AsyncSession,
    *,
    connection: Any = None,
    project_ids: list[uuid.UUID] | None = None,
    now: datetime | None = None,
    drop_partitions: bool = True,
) -> JobReport:
    """Delete data past its retention window.

    Payload rows go per project, because payload retention is per project. Partitions go
    globally using the **longest** window across all projects: a partition holds every
    project's traces for that month, so dropping it on behalf of the shortest window would
    destroy another tenant's retainable data.
    """
    report = JobReport(job="sweep_retention")
    service = RetentionService(session)

    for project_id in project_ids if project_ids is not None else await active_project_ids(session):
        project = await session.get(Project, project_id)
        if project is None:
            continue
        deleted = await service.sweep_payload_rows(
            project_id=project_id, days=project.retention_days_payloads, now=now
        )
        report.processed += deleted
        if deleted:
            report.projects += 1

    if drop_partitions and connection is not None:
        longest = await service.longest_trace_retention_days()
        outcome = await drop_expired_partitions(connection, retention_days=longest, now=now)
        report.detail["partitions_dropped"] = outcome.partitions_dropped
        report.detail["retention_days_used"] = longest
        report.detail["notes"] = outcome.notes

    return report


async def queue_health(session: AsyncSession, *, project_id: uuid.UUID) -> dict[str, Any]:
    """Per-queue depth and the age of the oldest pending item.

    Age matters more than depth. A queue of 500 items all raised this morning is a busy day;
    a queue of 5 items where the oldest is three weeks old means nobody is reading it, and
    that is the state in which a review queue stops being a control.
    """
    rows = (
        await session.execute(
            select(
                ReviewQueue.slug,
                ReviewAssignment.status,
                func.count(),
                func.min(ReviewAssignment.created_at),
            )
            .join(ReviewAssignment, ReviewAssignment.queue_id == ReviewQueue.id)
            .where(
                ReviewQueue.project_id == project_id,
                ReviewQueue.deleted_at.is_(None),
            )
            .group_by(ReviewQueue.slug, ReviewAssignment.status)
        )
    ).all()

    health: dict[str, Any] = {}
    for slug, status, count, oldest in rows:
        entry = health.setdefault(str(slug), {"oldest_pending": None})
        entry[str(status)] = int(count)
        if status == "pending":
            entry["oldest_pending"] = oldest.isoformat() if oldest else None
    return health


__all__ = [
    "JobReport",
    "active_project_ids",
    "queue_health",
    "release_expired_leases",
    "rollup_online_metrics",
    "run_online_eval",
    "sweep_retention",
]
