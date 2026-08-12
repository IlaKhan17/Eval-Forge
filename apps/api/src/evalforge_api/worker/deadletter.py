"""Recording and reading permanently-failed background jobs.

The rule this module encodes: **a job may fail, but a failure may not be silent.** arq retries
a job a few times and then drops it, which is correct behaviour and invisible behaviour. Every
consequence of a dropped job here is an absence — no online evaluations, no lease recovery, no
retention sweep — and absence is indistinguishable from a quiet day.

Two properties worth stating, because both were deliberate:

- **Recording a dead letter uses its own session.** The failing job's session is being rolled
  back; writing the record on it would be rolled back with it. That is the single most common
  way a dead-letter table ends up permanently empty.
- **Recording a dead letter must never raise.** It runs in an exception handler, and an
  exception thrown from there replaces a diagnosable job failure with a confusing one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from evalforge_api.db.base import uuid7
from evalforge_api.db.models.ops import MAX_MESSAGE_CHARS, DeadLetterJob, WorkerHeartbeat
from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("evalforge.worker.deadletter")

# Core Table rather than the ORM class, so the upsert can name the conflict target directly.
HEARTBEATS = cast("Table", WorkerHeartbeat.__table__)

#: Keys allowed into the stored `context`. An allow-list rather than "everything except
#: secrets": these jobs take ids, limits, and windows, so enumerating what is kept is both
#: short and safe, while a deny-list would silently start storing whatever a future job's
#: keyword argument happens to be.
SAFE_CONTEXT_KEYS = frozenset(
    {"project_id", "project_ids", "batch_size", "limit", "window_hours", "hours", "queue_id"}
)


@dataclass(slots=True)
class QueueSnapshot:
    """What the job queue looks like right now."""

    #: None when Redis could not be reached — distinct from 0, which means an empty queue.
    #: Reporting a failed probe as zero would read as "nothing is backed up".
    depth: int | None = None
    scheduled: int | None = None
    in_progress: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "depth": self.depth,
            "scheduled": self.scheduled,
            "in_progress": self.in_progress,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def _safe_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    return {key: _stringify(value) for key, value in context.items() if key in SAFE_CONTEXT_KEYS}


def _stringify(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [_stringify(item) for item in value]
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return repr(value)[:200]


async def record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_name: str,
    error: BaseException,
    attempts: int = 1,
    job_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """Store one dead letter. Returns its id, or None if it could not be stored.

    Swallows its own failures on purpose — see the module docstring. If the database is the
    reason the job failed, this write will fail too, and the log line below is then the only
    record. That is an acceptable floor: the alternative is an exception that masks the
    original one.
    """
    try:
        async with session_factory() as session:
            row = DeadLetterJob(
                job_name=job_name,
                job_id=job_id,
                attempts=attempts,
                error_type=type(error).__name__,
                # Truncated, and only the message — never the traceback. See models/ops.py.
                error_message=str(error)[:MAX_MESSAGE_CHARS] or "(no message)",
                context=_safe_context(context),
            )
            session.add(row)
            await session.commit()
            return row.id
    except Exception:
        logger.exception(
            "could not record a dead letter for job %s; the original failure was %s: %s",
            job_name,
            type(error).__name__,
            error,
        )
        return None


async def beat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker_name: str = "default",
    detail: dict[str, Any] | None = None,
) -> None:
    """Record that this worker is alive.

    Upserted on the worker name, so the table holds one row per worker rather than a growing log of
    minutes. Failures are swallowed and logged for the same reason as `record`: a heartbeat that can
    take down a worker turns a monitoring feature into an outage.
    """
    try:
        async with session_factory() as session:
            await session.execute(
                pg_insert(HEARTBEATS)
                .values(
                    id=uuid7(),
                    worker_name=worker_name,
                    last_seen_at=datetime.now(UTC),
                    detail=detail or {},
                )
                .on_conflict_do_update(
                    index_elements=["worker_name"],
                    set_={"last_seen_at": datetime.now(UTC), "detail": detail or {}},
                )
            )
            await session.commit()
    except Exception:
        logger.exception("could not record a heartbeat for worker %s", worker_name)


async def heartbeats(session: AsyncSession) -> list[WorkerHeartbeat]:
    """Every worker's last beat, oldest first — the order an operator wants to read."""
    return list(
        (
            await session.execute(
                select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def unresolved(
    session: AsyncSession, *, limit: int = 50, since: datetime | None = None
) -> list[DeadLetterJob]:
    query = select(DeadLetterJob).where(DeadLetterJob.resolved_at.is_(None))
    if since is not None:
        query = query.where(DeadLetterJob.created_at >= since)
    query = query.order_by(DeadLetterJob.created_at.desc()).limit(limit)
    return list((await session.execute(query)).scalars().all())


async def resolve(session: AsyncSession, dead_letter_id: uuid.UUID, *, note: str) -> bool:
    """Mark one dead letter handled. Returns False if it was already resolved or absent.

    The row is kept rather than deleted, so "this job has failed on eleven separate days"
    stays answerable after each one is dealt with.
    """
    result = await session.execute(
        update(DeadLetterJob)
        .where(DeadLetterJob.id == dead_letter_id, DeadLetterJob.resolved_at.is_(None))
        .values(resolved_at=datetime.now(UTC), resolution=note)
        .returning(DeadLetterJob.id)
    )
    return result.scalar_one_or_none() is not None


async def summary(session: AsyncSession, *, window_hours: int = 24) -> dict[str, Any]:
    """Counts by job over a window, plus the age of the oldest unresolved failure.

    Age, not just count, for the same reason the review-queue health check reports it: one
    failure this minute is an incident, one failure unresolved for three weeks is a job
    nobody is watching, and the count alone cannot tell them apart.
    """
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    rows = (
        await session.execute(
            select(DeadLetterJob.job_name, func.count(), func.max(DeadLetterJob.created_at))
            .where(DeadLetterJob.created_at >= since)
            .group_by(DeadLetterJob.job_name)
        )
    ).all()

    oldest = (
        await session.execute(
            select(func.min(DeadLetterJob.created_at)).where(DeadLetterJob.resolved_at.is_(None))
        )
    ).scalar_one_or_none()
    total_unresolved = (
        await session.execute(
            select(func.count())
            .select_from(DeadLetterJob)
            .where(DeadLetterJob.resolved_at.is_(None))
        )
    ).scalar_one()

    return {
        "window_hours": window_hours,
        "by_job": {
            str(name): {"failures": int(count), "last_failure": last.isoformat()}
            for name, count, last in rows
        },
        "unresolved": int(total_unresolved),
        "oldest_unresolved": oldest.isoformat() if oldest else None,
    }


async def queue_snapshot(redis_url: str, *, queue_name: str = "arq:queue") -> QueueSnapshot:
    """Depth of the arq job queue, from Redis.

    Cheap by construction: `ZCARD` and `ZCOUNT` on one key, and a bounded `SCAN` for the
    in-progress markers. Deliberately not `queued_jobs()`, which deserialises every queued job
    to answer a question about how many there are.

    A Redis that cannot be reached is reported as an error, not as an empty queue. Those two
    states look identical in a `0` and mean opposite things.
    """
    snapshot = QueueSnapshot()
    try:
        from arq.connections import RedisSettings, create_pool  # noqa: PLC0415 — optional at import

        pool = await create_pool(RedisSettings.from_dsn(redis_url))
        try:
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            snapshot.depth = int(await pool.zcard(queue_name))
            # arq scores queued jobs with their earliest run time, so anything scored in the
            # future is deferred rather than backed up. Counting them together would make a
            # healthy schedule of cron jobs look like a backlog.
            snapshot.scheduled = int(await pool.zcount(queue_name, now_ms, "+inf"))
            in_progress = 0
            async for _ in pool.scan_iter(match="arq:in-progress:*", count=100):
                in_progress += 1
            snapshot.in_progress = in_progress
        finally:
            await pool.aclose()
    except Exception as exc:  # an unreachable queue is a reportable state, not a crash
        snapshot.error = f"{type(exc).__name__}: {exc}"[:200]
    return snapshot


__all__ = [
    "SAFE_CONTEXT_KEYS",
    "QueueSnapshot",
    "beat",
    "heartbeats",
    "queue_snapshot",
    "record",
    "resolve",
    "summary",
    "unresolved",
]
