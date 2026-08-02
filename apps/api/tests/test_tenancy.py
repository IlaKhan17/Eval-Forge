"""Tenant isolation against a real database.

Layer one of the three-layer defence (docs/DATABASE_DESIGN.md §3): the repository
injects the predicate so a route author cannot forget it. These tests exist because
the consequence of getting it wrong is one customer reading another's data, which is
the single worst failure this system can have.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import Tenant, make_tenant
from evalforge_api.db.models.identity import ApiKey, Environment, Project
from evalforge_api.repositories.base import TenantContext, TenantRepository
from evalforge_api.security import keys as key_utils
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


class EnvironmentRepo(TenantRepository[Environment]):
    model = Environment


class ApiKeyRepo(TenantRepository[ApiKey]):
    model = ApiKey


class ProjectRepo(TenantRepository[Project]):
    model = Project
    tenant_column = "org_id"


def repo_for(session: AsyncSession, tenant: Tenant) -> EnvironmentRepo:
    return EnvironmentRepo(session, TenantContext(project_id=tenant.project.id))


class TestRepositoryScoping:
    async def test_a_tenant_sees_its_own_rows(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        found = await repo_for(session, tenant_a).get(tenant_a.environment.id)
        assert found is not None
        assert found.id == tenant_a.environment.id

    async def test_a_tenant_cannot_read_another_tenants_row_by_id(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        """The core IDOR test. Knowing the UUID must not be enough."""
        assert await repo_for(session, tenant_a).get(tenant_b.environment.id) is None

    async def test_listing_never_crosses_the_boundary(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        rows = await repo_for(session, tenant_a).list_all()
        ids = {row.id for row in rows}
        assert tenant_a.environment.id in ids
        assert tenant_b.environment.id not in ids

    async def test_exists_does_not_leak_across_tenants(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        """`exists` returning True for a foreign row would confirm it exists."""
        repo = repo_for(session, tenant_a)
        assert await repo.exists(tenant_a.environment.id)
        assert not await repo.exists(tenant_b.environment.id)

    async def test_insert_stamps_the_tenant_rather_than_trusting_the_caller(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        """Even a caller who sets the wrong project_id gets their own tenant."""
        repo = repo_for(session, tenant_a)
        smuggled = Environment(project_id=tenant_b.project.id, name="smuggled")
        repo.add(smuggled)
        await session.flush()
        assert smuggled.project_id == tenant_a.project.id

    async def test_a_repository_without_its_tenant_refuses_rather_than_defaulting(
        self, session: AsyncSession
    ) -> None:
        """Silently returning every row would be the worst possible fallback."""
        repo = EnvironmentRepo(session, TenantContext(org_id=uuid.uuid4()))
        with pytest.raises(PermissionError, match="project_id"):
            await repo.get(uuid.uuid4())

    async def test_org_scoped_repositories_work_too(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        repo = ProjectRepo(session, TenantContext(org_id=tenant_a.org.id))
        assert await repo.get(tenant_a.project.id) is not None
        assert await repo.get(tenant_b.project.id) is None

    async def test_a_context_needs_at_least_one_tenant(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            TenantContext()


class TestApiKeyStorage:
    async def test_only_the_digest_is_persisted(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """A database dump must not yield a usable credential."""
        row = await session.get(ApiKey, tenant_a.api_key.id)
        assert row is not None
        assert len(row.key_hash) == 32
        secret = tenant_a.token.split("_", 3)[3]
        assert secret not in row.prefix
        assert secret.encode() not in row.key_hash

    async def test_a_key_is_found_by_prefix_and_verified_by_digest(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        prefix = key_utils.parse_prefix(tenant_a.token)
        result = await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        row = result.scalar_one()
        assert key_utils.verify(tenant_a.token, row.key_hash)

    async def test_another_tenants_token_does_not_verify(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        row = await session.get(ApiKey, tenant_a.api_key.id)
        assert row is not None
        assert not key_utils.verify(tenant_b.token, row.key_hash)

    async def test_prefixes_are_globally_unique(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Two keys sharing a prefix would make lookup ambiguous."""
        duplicate = ApiKey(
            project_id=tenant_a.project.id,
            prefix=tenant_a.api_key.prefix,
            key_hash=b"\x01" * 32,
            scopes=["read"],
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_a_revoked_key_is_not_usable(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        from datetime import UTC, datetime

        row = await session.get(ApiKey, tenant_a.api_key.id)
        assert row is not None
        now = datetime.now(UTC)
        assert row.is_usable(now=now)
        row.revoked_at = now
        assert not row.is_usable(now=now)

    async def test_an_expired_key_is_not_usable(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        from datetime import UTC, datetime, timedelta

        row = await session.get(ApiKey, tenant_a.api_key.id)
        assert row is not None
        now = datetime.now(UTC)
        row.expires_at = now - timedelta(seconds=1)
        assert not row.is_usable(now=now)


class TestSchemaConstraints:
    async def test_project_defaults_are_conservative(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Safe defaults are a security control: capture redacted, retention short."""
        project = await session.get(Project, tenant_a.project.id)
        assert project is not None
        assert project.default_capture_mode == "redacted"
        assert project.retention_days_traces == 30
        assert project.retention_days_payloads == 14

    async def test_an_invalid_capture_mode_is_rejected_by_the_database(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The CHECK constraint is the backstop for a bug in the application layer."""
        project = await session.get(Project, tenant_a.project.id)
        assert project is not None
        project.default_capture_mode = "everything"
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_an_invalid_role_is_rejected(self, session: AsyncSession) -> None:
        from evalforge_api.db.models.identity import Membership

        tenant = await make_tenant(session, slug="gamma")
        session.add(Membership(org_id=tenant.org.id, user_id=tenant.user.id, role="superuser"))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_a_sample_rate_outside_zero_to_one_is_rejected(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        project = await session.get(Project, tenant_a.project.id)
        assert project is not None
        project.online_eval_sample_rate = 5.0
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_environment_names_are_unique_per_project(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        session.add(Environment(project_id=tenant_a.project.id, name="production"))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_the_same_environment_name_is_fine_in_another_project(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        session.add(Environment(project_id=tenant_b.project.id, name="staging"))
        session.add(Environment(project_id=tenant_a.project.id, name="staging"))
        await session.flush()  # must not raise

    async def test_ids_are_time_ordered(self, session: AsyncSession) -> None:
        """UUIDv7 keeps inserts local in the index instead of scattering them."""
        first = await make_tenant(session, slug="t1")
        second = await make_tenant(session, slug="t2")
        assert first.project.id.hex < second.project.id.hex
