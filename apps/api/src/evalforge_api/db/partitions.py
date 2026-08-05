"""Monthly partition management for the trace tables.

Partitions are not self-creating. An insert whose timestamp falls outside every
defined range fails outright, so a partitioned table without a maintenance job is a
scheduled outage — it works until the first of the month and then rejects everything.

Two safeguards against that:

- `ensure_partitions` creates the current month plus several ahead, and is called at
  startup and by the retention job.
- A `DEFAULT` partition catches anything that still falls through, so a missing
  range degrades to "rows land in the wrong partition" instead of "ingestion is
  down". Rows in the default partition are reported, because they mean the
  maintenance job is behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

PARTITIONED_TABLES: tuple[tuple[str, str], ...] = (
    ("traces", "started_at"),
    ("spans", "started_at"),
    ("span_events", "timestamp"),
)

MONTHS_AHEAD = 3


def is_partition_child(name: str) -> bool:
    """True for a partition of one of the partitioned tables.

    Autogenerate walks every relation Postgres reports, so without this it sees
    `traces_2026_08` as a table nobody declared and proposes dropping it — turning
    partition maintenance into permanent, alarming schema drift.
    """
    return any(name.startswith(f"{table}_") for table, _ in PARTITIONED_TABLES)


@dataclass(frozen=True, slots=True)
class MonthRange:
    year: int
    month: int

    @property
    def suffix(self) -> str:
        return f"{self.year:04d}_{self.month:02d}"

    @property
    def start(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-01"

    @property
    def end(self) -> str:
        year, month = (self.year + 1, 1) if self.month == 12 else (self.year, self.month + 1)
        return f"{year:04d}-{month:02d}-01"

    def next(self) -> MonthRange:
        return (
            MonthRange(self.year + 1, 1)
            if self.month == 12
            else MonthRange(self.year, self.month + 1)
        )

    @classmethod
    def of(cls, moment: datetime) -> MonthRange:
        return cls(moment.year, moment.month)


def partition_statements(table: str, month: MonthRange) -> str:
    name = f"{table}_{month.suffix}"
    return (
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
        f"FOR VALUES FROM ('{month.start}') TO ('{month.end}')"
    )


def default_partition_statement(table: str) -> str:
    return f"CREATE TABLE IF NOT EXISTS {table}_default PARTITION OF {table} DEFAULT"


async def ensure_partitions(
    connection: AsyncConnection, *, now: datetime | None = None, months_ahead: int = MONTHS_AHEAD
) -> list[str]:
    """Create the current month and the next few for every partitioned table."""
    moment = now or datetime.now(UTC)
    created: list[str] = []

    for table, _ in PARTITIONED_TABLES:
        month = MonthRange.of(moment)
        for _ in range(months_ahead + 1):
            await connection.execute(text(partition_statements(table, month)))
            created.append(f"{table}_{month.suffix}")
            month = month.next()
        await connection.execute(text(default_partition_statement(table)))

    # RLS on the parent covers queries *through* the parent, which is how the application reads.
    # It does not give a newly attached partition its own policy, so a direct query on the child
    # would bypass it. Applied here rather than only in the migration because partitions are
    # created at every startup, months after the migration ran.
    from evalforge_api.db.rls import apply_policies  # noqa: PLC0415 — avoids an import cycle

    await apply_policies(
        connection, [*created, *(f"{table}_default" for table, _ in PARTITIONED_TABLES)]
    )
    return created


async def default_partition_counts(connection: AsyncConnection) -> dict[str, int]:
    """Rows that landed in a DEFAULT partition.

    Non-zero means partition maintenance fell behind. Worth surfacing: the data is
    safe, but retention cannot drop it cheaply and queries lose partition pruning.
    """
    counts: dict[str, int] = {}
    for table, _ in PARTITIONED_TABLES:
        result = await connection.execute(text(f"SELECT count(*) FROM {table}_default"))  # noqa: S608
        counts[table] = int(result.scalar_one())
    return counts


async def drop_partitions_before(
    connection: AsyncConnection, table: str, *, cutoff: datetime
) -> list[str]:
    """Retention by partition drop: instant, and it reclaims the space immediately.

    A bulk DELETE would leave dead tuples for autovacuum, bloat the indexes, and
    take proportionally longer as the table grows.
    """
    result = await connection.execute(
        text(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = :table AND c.relname <> :default_name
            """
        ),
        {"table": table, "default_name": f"{table}_default"},
    )

    dropped: list[str] = []
    boundary = MonthRange.of(cutoff).suffix
    for (name,) in result.all():
        suffix = name.removeprefix(f"{table}_")
        if len(suffix) == len(boundary) and suffix < boundary:
            await connection.execute(text(f"DROP TABLE IF EXISTS {name}"))
            dropped.append(name)
    return dropped


async def missing_partitions(
    connection: AsyncConnection, *, now: datetime | None = None
) -> list[str]:
    """Partitioned tables with no partition covering the current month.

    A read-only check, so the application can run it without DDL privileges. It deliberately does
    not consider the DEFAULT partition sufficient: rows landing there are stored but unpartitioned,
    which defeats retention-by-drop and is exactly the state worth reporting.
    """
    moment = now or datetime.now(UTC)
    suffix = MonthRange.of(moment).suffix
    existing = {
        str(name)
        for name in (
            await connection.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relkind = 'r'"
                )
            )
        )
        .scalars()
        .all()
    }
    return [table for table, _ in PARTITIONED_TABLES if f"{table}_{suffix}" not in existing]
