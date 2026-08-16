"""Deleting data the project no longer wants to keep.

Retention is a security control as much as an operational one: data that no longer exists
cannot leak, and a project that promised 30-day retention has a compliance problem the
moment day 31 arrives with the rows still there.

The important decision is **how** old traces go away. Two mechanisms, used for different
things:

- **Drop whole partitions** for traces and spans. `DROP TABLE` on a partition is O(1) and
  reclaims the disk immediately. A `DELETE` over the same rows writes a tombstone per row,
  bloats the table, and leaves the space to be recovered by a `VACUUM FULL` that takes an
  exclusive lock — which is how a retention job becomes an outage.
- **Delete rows** for the small satellite tables, where the volume is low enough that the
  cost is irrelevant and partitioning would be complexity for nothing.

A partition is only dropped when its **entire range** is older than the retention window.
Dropping a partition that still holds retainable rows would delete data the project asked
to keep, which is far worse than keeping data slightly too long — so the boundary rounds in
the safe direction.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proofstep_api.db.models.identity import Project
from proofstep_api.db.models.traces import PayloadObject
from proofstep_api.db.partitions import PARTITIONED_TABLES

#: `traces_2026_03` — the suffix a partition carries.
_PARTITION_SUFFIX = re.compile(r"^(?P<table>[a-z_]+)_(?P<year>\d{4})_(?P<month>\d{2})$")


@dataclass(slots=True)
class RetentionOutcome:
    partitions_dropped: list[str] = field(default_factory=list)
    partitions_kept: list[str] = field(default_factory=list)
    payload_rows_deleted: int = 0
    payload_objects_orphaned: int = 0
    notes: list[str] = field(default_factory=list)


def month_start(name: str) -> datetime | None:
    """The first instant a partition holds, or None if the name is not a month partition.

    Returning None rather than raising matters: the default partition and any table an
    operator created by hand must be left alone, not treated as an unparseable error that
    aborts the sweep.
    """
    match = _PARTITION_SUFFIX.match(name)
    if match is None:
        return None
    try:
        return datetime(int(match.group("year")), int(match.group("month")), 1, tzinfo=UTC)
    except ValueError:
        return None


def month_end(start: datetime) -> datetime:
    return (
        datetime(start.year + 1, 1, 1, tzinfo=UTC)
        if start.month == 12
        else datetime(start.year, start.month + 1, 1, tzinfo=UTC)
    )


def droppable_partitions(
    names: list[str], *, cutoff: datetime, tables: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """Split partitions into those safe to drop and those to keep.

    A partition is droppable only when its **end** is at or before the cutoff, so every row
    it could hold is outside the retention window. Comparing the *start* instead would drop
    the current month on the first day of retention and delete data the project asked to
    keep — a data-loss bug that a test with a mid-month clock would not catch.
    """
    drop: list[str] = []
    keep: list[str] = []
    for name in sorted(names):
        start = month_start(name)
        if start is None or not any(name.startswith(f"{table}_") for table in tables):
            # The DEFAULT partition lands here, and that is correct: rows in it have no
            # known range, so nothing can be proven about their age.
            keep.append(name)
            continue
        (drop if month_end(start) <= cutoff else keep).append(name)
    return drop, keep


class RetentionService:
    """Applies one project's retention policy.

    Partition drops are global rather than per-project — a partition holds every project's
    traces for that month — so the sweep uses the **longest** retention window across all
    projects. A shared partition dropped on behalf of the shortest window would delete
    another project's retainable data, which is the kind of cross-tenant bug that ends
    trust in a platform.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def longest_trace_retention_days(self) -> int:
        longest = (
            (
                await self.session.execute(
                    select(Project.retention_days_traces).where(Project.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        return max([*longest, 1])

    async def sweep_payload_rows(
        self, *, project_id: uuid.UUID, days: int, now: datetime | None = None
    ) -> int:
        """Delete payload rows past their (shorter) retention window.

        Payloads have their own, shorter window than traces on purpose: the prompt bodies are
        the sensitive part, and the span skeleton stays useful for latency and cost analysis
        long after the text should be gone.
        """
        cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
        result = await self.session.execute(
            delete(PayloadObject)
            .where(
                PayloadObject.project_id == project_id,
                PayloadObject.created_at < cutoff,
            )
            .returning(PayloadObject.id)
        )
        return len(result.scalars().all())


async def drop_expired_partitions(
    connection: AsyncConnection, *, retention_days: int, now: datetime | None = None
) -> RetentionOutcome:
    """Drop every trace partition entirely older than the retention window.

    Runs on a raw connection: `DROP TABLE` is DDL and does not belong in a session that also
    holds ORM state.
    """
    outcome = RetentionOutcome()
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    tables = tuple(table for table, _ in PARTITIONED_TABLES)

    existing = (
        (
            await connection.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relkind = 'r' "
                    "AND c.relispartition"
                )
            )
        )
        .scalars()
        .all()
    )

    drop, keep = droppable_partitions(
        [str(name) for name in existing], cutoff=cutoff, tables=tables
    )
    outcome.partitions_kept = keep

    for name in drop:
        # Identifier interpolation is unavoidable for DDL, so the name is proven to have come
        # from pg_class and to match the partition-name pattern before it reaches here.
        if month_start(name) is None:
            continue
        await connection.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        outcome.partitions_dropped.append(name)

    if drop:
        outcome.notes.append(
            f"dropped {len(drop)} partition(s) whose entire range predates "
            f"{cutoff.date().isoformat()}"
        )
    return outcome


async def count_orphaned_payload_objects(session: AsyncSession) -> int:
    """Payload rows whose spans are gone.

    Dropping a trace partition removes the spans that referenced a payload but not the
    payload row or its stored object, so this is the number that says how much object storage
    is being paid for and no longer reachable. Reported rather than deleted here: the object
    store is the authority on what actually exists, and deleting rows first would lose the
    only record of what to delete there.
    """
    from proofstep_api.db.models.traces import Span  # noqa: PLC0415 — avoids a cycle

    referenced = select(Span.input_ref).where(Span.input_ref.is_not(None))
    return len(
        (await session.execute(select(PayloadObject.id).where(PayloadObject.id.not_in(referenced))))
        .scalars()
        .all()
    )


__all__ = [
    "RetentionOutcome",
    "RetentionService",
    "count_orphaned_payload_objects",
    "drop_expired_partitions",
    "droppable_partitions",
    "month_end",
    "month_start",
]
