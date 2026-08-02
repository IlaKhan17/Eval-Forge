"""Request-scoped dependencies: session, principal, permission checks.

Authentication resolves a `Principal` from *one* credential source and derives the
tenant from it. The request body is never consulted for a project id — a
client-supplied tenant identifier that reaches a query is among the most common
multi-tenant breaches, so the shape of this module makes it impossible rather than
merely discouraged.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge_api.db.models.identity import ApiKey, Membership, Project, User
from evalforge_api.db.session import get_sessionmaker
from evalforge_api.errors import ForbiddenError, NotFoundError, UnauthorizedError
from evalforge_api.repositories.base import TenantContext
from evalforge_api.security import keys as key_utils
from evalforge_api.security import tokens
from evalforge_api.security.permissions import (
    Permission,
    Principal,
    permissions_for_role,
    permissions_for_scopes,
)
from evalforge_api.settings import Settings, get_settings


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Provide a bearer token in the Authorization header.")
    return token.strip()


async def get_principal(request: Request, session: SessionDep, settings: SettingsDep) -> Principal:
    """Resolve the caller from an API key or a session JWT."""
    token = _bearer(request)

    if key_utils.parse_prefix(token) is not None:
        principal = await _principal_from_api_key(token, session)
    else:
        principal = await _principal_from_jwt(token, session, settings)

    request.state.principal = principal
    return principal


async def _principal_from_api_key(token: str, session: AsyncSession) -> Principal:
    prefix = key_utils.parse_prefix(token)
    result = await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
    record = result.scalar_one_or_none()

    # Verify even when the prefix is unknown, against a dummy digest, so a valid
    # prefix and an invalid one take the same time. Otherwise response latency
    # enumerates which prefixes exist.
    expected = record.key_hash if record is not None else b"\x00" * 32
    matched = key_utils.verify(token, expected)

    if record is None or not matched:
        raise UnauthorizedError("The API key is not valid.")
    if not record.is_usable(now=datetime.now(UTC)):
        raise UnauthorizedError("The API key has been revoked or has expired.")

    project = await session.get(Project, record.project_id)
    if project is None or project.is_deleted:
        raise UnauthorizedError("The project for this key no longer exists.")

    return Principal(
        kind="api_key",
        id=str(record.id),
        permissions=permissions_for_scopes(record.scopes),
        org_id=project.org_id,
        project_id=record.project_id,
        environment_id=record.environment_id,
        scopes=tuple(record.scopes),
    )


async def _principal_from_jwt(token: str, session: AsyncSession, settings: Settings) -> Principal:
    claims = tokens.decode_access_token(token, secret=settings.jwt_secret)
    user = await session.get(User, uuid.UUID(claims.subject))
    if user is None or not user.is_active:
        raise UnauthorizedError("The session is no longer valid.")

    return Principal(kind="user", id=str(user.id), permissions=frozenset())


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def resolve_project(
    project_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> Principal:
    """Bind a user principal to a specific project, granting their role's permissions.

    Cross-tenant access raises 404, never 403: a 403 would confirm the project
    exists, which is itself information the caller is not entitled to.
    """
    if principal.kind == "api_key":
        if principal.project_id != project_id:
            raise NotFoundError("No such project.")
        return principal

    project = await session.get(Project, project_id)
    if project is None or project.is_deleted:
        raise NotFoundError("No such project.")

    membership = await session.execute(
        select(Membership).where(
            Membership.org_id == project.org_id,
            Membership.user_id == uuid.UUID(principal.id),
        )
    )
    record = membership.scalar_one_or_none()
    if record is None:
        raise NotFoundError("No such project.")

    return Principal(
        kind=principal.kind,
        id=principal.id,
        permissions=permissions_for_role(record.role),
        org_id=project.org_id,
        project_id=project.id,
        role=record.role,
    )


ProjectPrincipalDep = Annotated[Principal, Depends(resolve_project)]


def require(permission: Permission) -> Callable[[Principal], Awaitable[Principal]]:
    """Guard a route with a single permission from the central matrix."""

    async def guard(principal: ProjectPrincipalDep) -> Principal:
        if not principal.can(permission):
            raise ForbiddenError(
                f"This action requires the {permission.value!r} permission. "
                f"Your role is {principal.role or 'api key'} with "
                f"{'scopes ' + ','.join(principal.scopes) if principal.scopes else 'no scopes'}."
            )
        return principal

    return guard


def tenant_of(principal: Principal) -> TenantContext:
    return TenantContext(org_id=principal.org_id, project_id=principal.project_id)
