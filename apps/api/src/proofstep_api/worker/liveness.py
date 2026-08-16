"""Is this worker alive? Exit 0 if yes, 1 if not.

    python -m proofstep_api.worker.liveness

The worker serves no HTTP, so the container image's HTTP health check — written for the API, and
inherited by every container built from the same image — asks it a question it can never answer.
The result was a worker that ran perfectly and reported `unhealthy` forever: an orchestrator
configured to act on health would restart a working process in a loop, and `docker compose up
--wait` never returns.

Liveness here means "the heartbeat cron has run recently". That is a real signal rather than a
proxy for one: `heartbeat` is scheduled every thirty seconds precisely so that a quiet night is
distinguishable from a dead worker, and if the arq event loop is wedged the row stops advancing
even though the process is still resident. A check on the *process* would miss exactly that case,
which is the one worth catching.

Deliberately tolerant. `STALE_AFTER` is several times the heartbeat interval, because a check that
fires on one missed beat turns a slow database moment into a restart, and restarting a worker
mid-evaluation costs a paid model call that then runs again.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from proofstep_api.db.models.ops import WorkerHeartbeat
from proofstep_api.settings import get_settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

#: How old the newest heartbeat may be before this worker counts as dead. The cron runs every 30
#: seconds; this allows four misses.
STALE_AFTER = timedelta(seconds=120)

DEFAULT_WORKER_NAME = "default"


async def check(worker_name: str = DEFAULT_WORKER_NAME) -> tuple[bool, str]:
    settings = get_settings()
    # A fresh engine per invocation, disposed immediately. This runs as a short-lived probe process,
    # so a pool would be created and torn down for a single query either way.
    engine = create_async_engine(settings.sqlalchemy_url, pool_pre_ping=False)
    try:
        async with engine.connect() as connection:
            last_seen = (
                await connection.execute(
                    select(WorkerHeartbeat.last_seen_at).where(
                        WorkerHeartbeat.worker_name == worker_name
                    )
                )
            ).scalar_one_or_none()
    finally:
        await engine.dispose()

    if last_seen is None:
        # No row yet. The heartbeat cron has `run_at_startup=True`, so this is the window between
        # the process starting and its first beat — which is what a startup grace period is for, and
        # why this returns "not yet" rather than "dead".
        return False, "no heartbeat recorded yet"

    age = datetime.now(UTC) - last_seen
    if age > STALE_AFTER:
        return False, f"last heartbeat was {age.total_seconds():.0f}s ago"
    return True, f"last heartbeat {age.total_seconds():.0f}s ago"


def main() -> int:
    worker_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WORKER_NAME
    try:
        alive, detail = asyncio.run(check(worker_name))
    except Exception as error:
        # Unreachable database is not the worker being dead, but from a health check's point of view
        # a worker that cannot reach the database cannot do any work either. Reported distinctly so
        # the logs say which of the two it was.
        print(f"worker {worker_name}: cannot check ({type(error).__name__}: {error})")
        return 1
    print(f"worker {worker_name}: {detail}")
    return 0 if alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
