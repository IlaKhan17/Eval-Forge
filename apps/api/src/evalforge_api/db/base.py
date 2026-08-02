"""Declarative base, id generation, and shared column conventions."""

from __future__ import annotations

import os
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming so Alembic autogenerate produces stable, reviewable migrations
# instead of database-assigned names that differ between environments.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562 v7).

    Chosen over uuid4 for index locality: random v4 primary keys scatter inserts
    across the whole B-tree, which on an append-heavy table like `spans` means
    constant page splits. Chosen over bigserial because a sequential integer leaks
    row counts and invites enumeration across a tenant boundary.

    Python's stdlib has no uuid7 yet, so this is a small local implementation.
    """
    unix_ms = int(time.time() * 1000)
    rand = secrets.token_bytes(10)
    raw = bytearray(unix_ms.to_bytes(6, "big") + rand)
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} {identifier}>"


class IdentifiedBase(Base):
    """Every table shares one primary-key definition.

    Declaring `id` in one place also gives the repository layer a bound it can rely
    on, instead of every generic lookup asserting the attribute exists.
    """

    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UpdatedAtMixin:
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Soft delete for user-facing containers.

    Projects and datasets are referenced by immutable experiments, so a hard delete
    would leave those references dangling and silently break reproducibility. Traces
    and payloads, by contrast, are hard-deleted under retention: for those the point
    is that the data is actually gone.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


def jsonb_default() -> dict[str, Any]:
    return {}


IN_TEST = bool(os.environ.get("PYTEST_CURRENT_TEST"))
