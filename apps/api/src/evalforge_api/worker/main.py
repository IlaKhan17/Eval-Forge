"""The arq worker entry point.

Thin by design. Every job's logic lives in `jobs.py` as a function taking a session, so it
can be called directly from a test or by an operator without a Redis, a queue, or a running
worker. This module only supplies the session, the schedule, and the error handling.

Scheduling choices, each with a reason:

- **Online evaluation every minute.** Fast enough that a policy violation reaches a review
  queue while the incident is still live, cheap enough that an empty run costs one query per
  rule.
- **Retention daily, in the small hours.** It drops partitions, which is DDL; doing that
  during peak traffic risks contending with ingestion for locks.
- **Lease recovery every five minutes.** `claim_next` already reclaims opportunistically, so
  this only has to cover queues nobody is working.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq.connections import RedisSettings
from arq.cron import CronJob, cron
from evalforge_api.db.session import get_sessionmaker, init_engine
from evalforge_api.settings import Settings, get_settings
from evalforge_api.worker import deadletter, jobs
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("evalforge.worker")


async def _with_session(
    name: str, run: Any, ctx: dict[Any, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Run one job in its own session and return its report.

    The session is per job, not per worker. A worker holding one session for its lifetime
    accumulates identity-map state and, worse, keeps a transaction open across jobs so one
    job's failure rolls back another's work.
    """
    async with _session_factory()() as session:
        try:
            report = await run(session, **kwargs)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            # Logged, dead-lettered on the final attempt, and always re-raised. Re-raising is
            # what lets arq retry; swallowing it would make a permanently broken job look like
            # a healthy one that does nothing.
            logger.exception("job %s failed", name)
            await _dead_letter(name, exc, ctx, kwargs)
            raise
    logger.info("job %s: %s", name, report)
    return {"job": name, **report.detail}


async def _dead_letter(
    name: str, error: BaseException, ctx: dict[Any, Any] | None, kwargs: dict[str, Any]
) -> None:
    """Record the failure once retries are exhausted.

    Only on the last attempt: dead-lettering every attempt would turn one broken job into
    `max_tries` rows and make "how many distinct failures were there" unanswerable — which is
    the question the table exists to answer.

    `job_try` is absent when a job is invoked directly rather than through the queue (a test,
    or an operator). In that case there is no retry to wait for, so the failure is final and
    recording it immediately is right.
    """
    # Absent is not 1. arq always sets `job_try`, so a missing one means the job was invoked
    # directly — by a test or by an operator — and nobody is going to retry it. Treating that as
    # attempt 1 would wait for a retry that never comes and lose the record entirely.
    attempt = ctx.get("job_try") if ctx else None
    if attempt is not None and int(attempt) < WorkerSettings.max_tries:
        logger.info(
            "job %s will be retried (attempt %s of %d)", name, attempt, WorkerSettings.max_tries
        )
        return
    await deadletter.record(
        _session_factory(),
        job_name=name,
        error=error,
        attempts=int(attempt) if attempt is not None else 1,
        job_id=str((ctx or {}).get("job_id") or "") or None,
        context=kwargs,
    )


def _session_factory() -> async_sessionmaker[AsyncSession]:
    """The process-wide session factory, initialising the engine on first use.

    The worker has no FastAPI lifespan to hook, so it initialises lazily rather than
    duplicating the API's startup sequence.
    """
    try:
        return get_sessionmaker()
    except RuntimeError:
        init_engine(get_settings())
        return get_sessionmaker()


async def online_eval(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:
    return await _with_session("online_eval", jobs.run_online_eval, ctx)


async def release_leases(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:
    return await _with_session("release_expired_leases", jobs.release_expired_leases, ctx)


async def rollup(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:
    return await _with_session("rollup_online_metrics", jobs.rollup_online_metrics, ctx)


async def partitions(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:
    """Create the partitions the coming months need. DDL, so it needs a privileged connection."""
    async with _session_factory()() as session:
        connection = await session.connection()
        try:
            report = await jobs.maintain_partitions(session, connection=connection)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.exception("job maintain_partitions failed")
            await _dead_letter("maintain_partitions", exc, ctx, {})
            raise
    logger.info("job maintain_partitions: %s", report)
    return {"job": "maintain_partitions", **report.detail}


async def retention(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:
    """Retention needs a raw connection as well as a session, for the DDL."""
    async with _session_factory()() as session:
        bind = session.get_bind()
        connection = await session.connection()
        try:
            report = await jobs.sweep_retention(session, connection=connection)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.exception("job retention failed")
            await _dead_letter("retention", exc, ctx, {})
            raise
    logger.info("job retention: %s (bind %s)", report, bind.engine.url.database)
    return {"job": "retention", **report.detail}


def redis_settings(settings: Settings | None = None) -> RedisSettings:
    return RedisSettings.from_dsn((settings or get_settings()).redis_url)


class WorkerSettings:
    """arq's configuration object, discovered by `arq evalforge_api.worker.main.WorkerSettings`."""

    functions: ClassVar[list[Any]] = [online_eval, release_leases, rollup, retention, partitions]
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(online_eval, second=0, run_at_startup=True),
        cron(release_leases, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, second=30),
        cron(rollup, minute={0, 15, 30, 45}, second=10),
        # 03:17 rather than 03:00: a job scheduled on the hour competes with every other
        # system scheduled on the hour, and retention holds DDL locks.
        cron(retention, hour=3, minute=17, second=0),
        # Daily and at startup. Ingestion into an uncovered month fails outright, so this must not
        # wait for a schedule after a deploy that crossed a month boundary.
        cron(partitions, hour=3, minute=5, second=0, run_at_startup=True),
    ]
    # One at a time. These jobs are database-bound, and running four concurrently mostly
    # produces lock contention with ingestion rather than throughput.
    max_jobs = 1
    job_timeout = 600
    keep_result = 3600

    # Retry, then dead-letter. Three attempts because the failures worth retrying here are
    # transient — a connection reset during a redeploy, a lock timeout behind a long
    # ingestion — and those clear in seconds. A genuine bug fails all three just as fast, and
    # `_dead_letter` then records it exactly once.
    #
    # Every job is idempotent (see jobs.py), which is the precondition that makes retrying
    # safe at all: retrying a non-idempotent job double-counts instead of recovering.
    max_tries = 3
    retry_jobs = True

    @staticmethod
    def redis_settings() -> RedisSettings:
        return redis_settings()


__all__ = [
    "WorkerSettings",
    "online_eval",
    "partitions",
    "redis_settings",
    "release_leases",
    "retention",
    "rollup",
]
