"""Alembic environment.

Imports the models so autogenerate sees the full metadata, and reads the database
URL from application settings rather than alembic.ini — one source of truth for
connection details.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from proofstep_api.db import models  # noqa: F401 — registers every table
from proofstep_api.db.base import Base
from proofstep_api.db.partitions import is_partition_child
from proofstep_api.settings import get_settings
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A caller may set the URL explicitly (the test suite does, to target its own
# database). Only fall back to application settings when they have not.
#
# `migration_url`, not `sqlalchemy_url`: DDL runs as a role that owns the schema, and the
# application deliberately connects as one that does not — a non-owner is subject to RLS even
# without FORCE, and cannot create a table that has no policy. Where no separate migration role is
# configured the two are the same string, so a single-role development install is unaffected.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().migration_url)
target_metadata = Base.metadata


def include_object(_object, name, type_, _reflected, _compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Hide partition children from autogenerate."""
    if type_ == "table" and name is not None and is_partition_child(name):
        return False
    return not (type_ == "index" and name is not None and is_partition_child(name))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
