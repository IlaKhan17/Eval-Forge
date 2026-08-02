"""Tenant-scoped data access.

The single rule this module exists to enforce: **no query reaches the database
without a tenant predicate.** Route handlers never touch a raw session; they go
through a repository constructed with a `TenantContext`, and the predicate is
injected by `scoped()` rather than remembered by the author.

This is layer one of three (docs/DATABASE_DESIGN.md §3). Layer two is the
registry-driven cross-tenant test suite; layer three is Postgres RLS in Phase 12.
Layer one is the one that has to be right, because the other two only catch
mistakes it already made.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge_api.db.base import IdentifiedBase

if TYPE_CHECKING:
    from collections.abc import Sequence


class TenantContext:
    """The tenant every query in a request is confined to."""

    __slots__ = ("org_id", "project_id")

    def __init__(
        self, *, org_id: uuid.UUID | None = None, project_id: uuid.UUID | None = None
    ) -> None:
        if org_id is None and project_id is None:
            msg = "a TenantContext needs at least one of org_id or project_id"
            raise ValueError(msg)
        self.org_id = org_id
        self.project_id = project_id

    def __repr__(self) -> str:
        return f"<TenantContext org={self.org_id} project={self.project_id}>"


class TenantRepository[M: IdentifiedBase]:
    """Base repository. Subclasses declare a model and a tenant column."""

    model: type[M]
    tenant_column: str = "project_id"

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    # ------------------------------------------------------------------ scoping

    def scoped(self, statement: Select[Any] | None = None) -> Select[Any]:
        """Every read starts here, so the predicate cannot be forgotten."""
        stmt = statement if statement is not None else select(self.model)
        return stmt.where(self._tenant_predicate())

    def _tenant_predicate(self) -> Any:
        column = getattr(self.model, self.tenant_column, None)
        if column is None:
            msg = (
                f"{type(self).__name__} declares tenant_column="
                f"{self.tenant_column!r}, which {self.model.__name__} does not have"
            )
            raise AttributeError(msg)

        value = self.tenant.project_id if self.tenant_column == "project_id" else self.tenant.org_id
        if value is None:
            # Refusing beats defaulting. A repository asked to scope by a tenant it
            # does not have would otherwise silently return another tenant's rows.
            msg = (
                f"{type(self).__name__} needs {self.tenant_column} but the request's "
                f"TenantContext has none ({self.tenant!r})"
            )
            raise PermissionError(msg)
        return column == value

    # -------------------------------------------------------------------- reads

    async def get(self, entity_id: uuid.UUID) -> M | None:
        """Fetch by id *within the tenant*.

        Returns None for a row belonging to someone else — indistinguishable from
        one that does not exist, which is what the API layer turns into a 404.
        Answering 403 here would confirm the row exists.
        """
        result = await self.session.execute(self.scoped().where(self.model.id == entity_id))
        return result.scalar_one_or_none()

    async def list_all(self, *, limit: int = 100) -> Sequence[M]:
        result = await self.session.execute(self.scoped().limit(limit))
        return result.scalars().all()

    async def exists(self, entity_id: uuid.UUID) -> bool:
        return await self.get(entity_id) is not None

    # ------------------------------------------------------------------- writes

    def add(self, entity: M) -> M:
        """Stamp the tenant on insert rather than trusting the caller to."""
        value = self.tenant.project_id if self.tenant_column == "project_id" else self.tenant.org_id
        if hasattr(entity, self.tenant_column):
            setattr(entity, self.tenant_column, value)
        self.session.add(entity)
        return entity

    async def delete(self, entity: M) -> None:
        await self.session.delete(entity)
