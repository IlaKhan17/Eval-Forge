"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from proofstep_api.settings import Settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker  # noqa: PLW0603 — one engine per process
    _engine = create_async_engine(
        settings.sqlalchemy_url,
        pool_pre_ping=True,  # a recycled connection killed by the DB must not 500
        pool_size=10,
        max_overflow=10,
        echo=settings.debug,
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        msg = "engine not initialised; call init_engine() during startup"
        raise RuntimeError(msg)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        msg = "sessionmaker not initialised; call init_engine() during startup"
        raise RuntimeError(msg)
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction, committed on success and rolled back on any exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
