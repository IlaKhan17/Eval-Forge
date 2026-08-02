"""Integration fixtures backed by the real Postgres from docker compose.

Not SQLite. The schema uses JSONB, ARRAY, INET, and partial indexes, none of which
SQLite has — a suite that passed against SQLite would be testing a different
database than the one we ship.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from evalforge_api.db.base import Base
from evalforge_api.db.partitions import ensure_partitions
from evalforge_api.settings import Settings
from factories import Tenant, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DB = "evalforge_test"


def _settings() -> Settings:
    return Settings(
        env="test",
        postgres_user=os.environ.get("POSTGRES_USER", "evalforge"),
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

    test_engine = create_async_engine(settings.sqlalchemy_url)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # `create_all` builds the partitioned parents but not their partitions —
        # those are created by the migration and by the startup hook. Without this
        # every insert fails with "no partition of relation found for row", which is
        # precisely why the DEFAULT partition exists as a safety net in production.
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
