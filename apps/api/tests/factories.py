"""Tenant factories for integration tests.

A uniquely named module rather than conftest: pytest puts every conftest on
sys.path under the same module name, so two packages that both define one collide
the moment their suites run together.
"""

from __future__ import annotations

import uuid

from proofstep_api.db.models.identity import (
    ApiKey,
    Environment,
    Membership,
    Organization,
    Project,
    User,
)
from proofstep_api.security import keys as key_utils
from sqlalchemy.ext.asyncio import AsyncSession


class Tenant:
    """A complete, isolated tenant: org, user, membership, project, key."""

    def __init__(
        self,
        org: Organization,
        user: User,
        project: Project,
        environment: Environment,
        api_key: ApiKey,
        token: str,
    ) -> None:
        self.org = org
        self.user = user
        self.project = project
        self.environment = environment
        self.api_key = api_key
        self.token = token


async def make_tenant(
    session: AsyncSession, *, slug: str, role: str = "owner", scopes: list[str] | None = None
) -> Tenant:
    org = Organization(name=f"Org {slug}", slug=slug)
    user = User(email=f"{slug}-{uuid.uuid4().hex[:6]}@example.com", name=f"User {slug}")
    session.add_all([org, user])
    await session.flush()

    membership = Membership(org_id=org.id, user_id=user.id, role=role)
    project = Project(org_id=org.id, name=f"Project {slug}", slug=slug)
    session.add_all([membership, project])
    await session.flush()

    environment = Environment(project_id=project.id, name="production")
    session.add(environment)
    await session.flush()

    generated = key_utils.generate("test")
    api_key = ApiKey(
        project_id=project.id,
        environment_id=environment.id,
        name="default",
        prefix=generated.prefix,
        key_hash=generated.key_hash,
        scopes=scopes if scopes is not None else ["ingest", "read", "write"],
        created_by=user.id,
    )
    session.add(api_key)
    await session.flush()

    return Tenant(org, user, project, environment, api_key, generated.token)
