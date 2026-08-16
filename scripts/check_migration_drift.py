"""Fail when the ORM models and the migration history disagree.

The most common Alembic mistake is editing a model and forgetting the migration.
It stays invisible until a deploy hits a column that does not exist, so the check
belongs in CI rather than in a reviewer's head.
"""

from __future__ import annotations

import asyncio
import sys

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from proofstep_api.db import models  # noqa: F401 — registers every table
from proofstep_api.db.base import Base
from proofstep_api.db.partitions import is_partition_child
from proofstep_api.settings import get_settings
from sqlalchemy.ext.asyncio import create_async_engine


def _include(_object, name, type_, _reflected, _compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Partition children are created by maintenance, not by migrations."""
    return not (name is not None and type_ in ("table", "index") and is_partition_child(name))


def _diff(connection) -> list:  # type: ignore[no-untyped-def]
    context = MigrationContext.configure(
        connection, opts={"include_object": _include, "compare_type": True}
    )
    return compare_metadata(context, Base.metadata)


async def main() -> int:
    engine = create_async_engine(get_settings().sqlalchemy_url)
    async with engine.connect() as connection:
        differences = await connection.run_sync(_diff)
    await engine.dispose()

    if differences:
        print("✗ models and migrations disagree:")
        for entry in differences:
            print(f"    {entry}")
        print("\n  Run: uv run alembic revision --autogenerate -m '<what changed>'")
        return 1
    print("✓ models and migrations agree")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
