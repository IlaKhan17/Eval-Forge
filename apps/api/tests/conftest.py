"""Integration fixtures backed by the real Postgres from docker compose.

Not SQLite. The schema uses JSONB, ARRAY, INET, and partial indexes, none of which
SQLite has — a suite that passed against SQLite would be testing a different
database than the one we ship.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from factories import Tenant, make_tenant
from proofstep_api.db.partitions import ensure_partitions
from proofstep_api.settings import Settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TEST_DB = "proofstep_test"
ROOT = Path(__file__).resolve().parents[3]


def _migrate(url: str) -> None:
    """Run alembic in a worker thread; env.py opens its own event loop."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "infra" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


def _settings() -> Settings:
    return Settings(
        env="test",
        postgres_user=os.environ.get("POSTGRES_USER", "proofstep"),
        postgres_password=os.environ.get("POSTGRES_PASSWORD", ""),
        postgres_host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
        postgres_db=TEST_DB,
        jwt_secret="test-secret-value-that-is-long-enough-32",
    )


def _admin_url(settings: Settings) -> str:
    return (
        f"postgresql+psycopg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/postgres"
    )


@pytest_asyncio.fixture(scope="session")
async def engine():  # type: ignore[no-untyped-def]
    settings = _settings()

    admin = create_async_engine(_admin_url(settings), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            from sqlalchemy import text

            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except Exception as exc:
        pytest.skip(f"postgres unavailable for integration tests: {exc}")
    finally:
        await admin.dispose()

    # Migrations, not `create_all`. They are not equivalent: the migrations also
    # create partitions and the dataset-immutability triggers, so a suite built with
    # `create_all` would be testing a schema we never ship — and would have silently
    # skipped every trigger assertion.
    await asyncio.to_thread(_migrate, settings.sqlalchemy_url)

    test_engine = create_async_engine(settings.sqlalchemy_url)
    async with test_engine.begin() as conn:
        # Mirrors the production startup hook, which adds the current month's
        # partitions on top of the DEFAULT one the migration creates.
        await ensure_partitions(conn)
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    """A session rolled back after each test, so tests cannot see each other."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
        await db.rollback()


@pytest_asyncio.fixture
async def tenant_a(session: AsyncSession) -> Tenant:
    return await make_tenant(session, slug="alpha")


@pytest_asyncio.fixture
async def tenant_b(session: AsyncSession) -> Tenant:
    return await make_tenant(session, slug="beta")


# --------------------------------------------------------- the role RLS actually applies to
#
# Here rather than in test_rls.py, where it started, because it is not only the policy tests that
# need it. Every integration test that runs as the session superuser is testing a configuration no
# production deployment uses, and the gap is not theoretical: signup inserted a row into a
# tenant-scoped table with no tenant set, which a superuser connection accepts and the real
# application role refuses. See test_accounts.py::TestUnderRowLevelSecurity.

#: A role with no privileges beyond what the application needs, and crucially neither SUPERUSER nor
#: BYPASSRLS. Created per session and dropped afterwards.
APP_ROLE = "proofstep_rls_probe"
APP_PASSWORD = "probe-only-not-a-secret"


@pytest_asyncio.fixture(scope="session")
async def unprivileged_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """An engine connected as a role that RLS actually applies to."""
    settings = _settings()
    async with engine.begin() as setup:
        await setup.execute(
            text(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') "
                f"THEN CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}' "
                "NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; END IF; END $$"
            )
        )
        # Exactly the grants an application needs: use the schema and read/write the tables. No
        # ownership, so `FORCE ROW LEVEL SECURITY` is not even required for the policies to bite —
        # though it is set anyway, because a deployment may well run as the owner.
        await setup.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        await setup.execute(
            text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
            )
        )
        await setup.execute(
            text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
        )

    url = (
        f"postgresql+psycopg://{APP_ROLE}:{APP_PASSWORD}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{TEST_DB}"
    )
    probe = create_async_engine(url)
    try:
        yield probe
    finally:
        await probe.dispose()
        async with engine.begin() as teardown:
            await teardown.execute(
                text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
            )
            await teardown.execute(
                text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
            )
            await teardown.execute(text(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}"))
            await teardown.execute(text(f"DROP ROLE IF EXISTS {APP_ROLE}"))
