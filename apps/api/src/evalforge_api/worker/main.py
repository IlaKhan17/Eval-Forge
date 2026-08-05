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
from evalforge_api.worker import jobs
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("evalforge.worker")


async def _with_session(name: str, run: Any, **kwargs: Any) -> dict[str, Any]:
    """Run one job in its own session and return its report.

    The session is per job, not per worker. A worker holding one session for its lifetime
    accumulates identity-map state and, worse, keeps a transaction open across jobs so one
    job's failure rolls back another's work.
    """
    async with _session_factory()() as session:
        try:
            report = await run(session, **kwargs)
            await session.commit()
        except Exception:
            await session.rollback()
            # Logged and re-raised: arq's retry and dead-letter handling is the right owner
            # of a failed job, and swallowing the exception here would make a permanently
            # broken job look like a healthy one that does nothing.
            logger.exception("job %s failed", name)
            raise
    logger.info("job %s: %s", name, report)
    return {"job": name, **report.detail}


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


async def online_eval(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:  # noqa: ARG001
    return await _with_session("online_eval", jobs.run_online_eval)


async def release_leases(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:  # noqa: ARG001
    return await _with_session("release_expired_leases", jobs.release_expired_leases)


async def rollup(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:  # noqa: ARG001
    return await _with_session("rollup_online_metrics", jobs.rollup_online_metrics)


async def partitions(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:  # noqa: ARG001
    """Create the partitions the coming months need. DDL, so it needs a privileged connection."""
    async with _session_factory()() as session:
        connection = await session.connection()
        try:
            report = await jobs.maintain_partitions(session, connection=connection)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("job maintain_partitions failed")
            raise
    logger.info("job maintain_partitions: %s", report)
    return {"job": "maintain_partitions", **report.detail}


async def retention(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, Any]:  # noqa: ARG001
    """Retention needs a raw connection as well as a session, for the DDL."""
    async with _session_factory()() as session:
        bind = session.get_bind()
        connection = await session.connection()
        try:
            report = await jobs.sweep_retention(session, connection=connection)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("job retention failed")
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
