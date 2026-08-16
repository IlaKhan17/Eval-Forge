"""Row-level security: does the database actually stop a cross-tenant read?

These tests exist because RLS has a uniquely bad failure mode. Every way it stops working —
a missing policy, `FORCE` left off, a new tenant-scoped table, a superuser connection — leaves the
application behaving exactly as before. Nothing breaks. You simply have no isolation at the
database layer, and the layer above is the only thing standing between two tenants.

So the tests below connect as a **deliberately unprivileged role**. The repository's default
`docker compose` role is a superuser, and superusers are exempt from every policy regardless of
`FORCE` — the first run of these policies against it changed nothing at all. A test suite that ran
as that role would pass while proving nothing, which is the trap this file is written to avoid.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from proofstep_api.db.rls import (
    PROTECTED_TABLES,
    TENANT_SETTING,
    UNPROTECTED_TABLES,
    role_bypasses_rls,
    verify_enforced,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration


async def seed_two_tenants(engine: AsyncEngine) -> tuple[uuid.UUID, uuid.UUID]:
    """Two projects with one trace each, inserted as the privileged role.

    Seeded with the owner connection on purpose: the point of the tests is what the *unprivileged*
    role can see, and creating the data through it would only prove the policy blocks its own
    writes.
    """
    org = uuid.uuid4()
    alpha, beta = uuid.uuid4(), uuid.uuid4()
    moment = datetime.now(UTC) - timedelta(minutes=5)

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organizations (id, name, slug) VALUES (:id, 'probe', :slug)"),
            {"id": org, "slug": f"probe-{org.hex[:8]}"},
        )
        for project, label in ((alpha, "alpha"), (beta, "beta")):
            await conn.execute(
                text(
                    # Raw SQL, so the ORM's Python-side defaults do not apply and the not-null
                    # columns must be supplied. Raw on purpose: these tests are about what the
                    # *database* permits, and the ORM would sit between the assertion and the
                    # thing asserted.
                    "INSERT INTO projects (id, org_id, name, slug, settings, "
                    "  default_capture_mode, retention_days_traces, retention_days_payloads, "
                    "  online_eval_sample_rate) "
                    "VALUES (:id, :org, :name, :slug, '{}'::jsonb, 'redacted', 30, 14, 0.01)"
                ),
                {"id": project, "org": org, "name": label, "slug": f"{label}-{project.hex[:8]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO traces (id, project_id, trace_id, name, status, started_at, "
                    "  span_count, dropped_span_count, total_tokens, total_cost, error_count, "
                    "  capture_mode, metadata, tags, state) "
                    "VALUES (:id, :project, :trace, :name, 'ok', :started, "
                    "  1, 0, 0, 0, 0, 'redacted', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)"
                ),
                {
                    "id": uuid.uuid4(),
                    "project": project,
                    "trace": f"{label}-trace-{project.hex[:8]}",
                    "name": f"{label}-run",
                    "started": moment,
                },
            )
    return alpha, beta


async def with_tenant(connection: AsyncConnection, project_id: uuid.UUID | None) -> None:
    value = str(project_id) if project_id else ""
    await connection.execute(text(f"SELECT set_config('{TENANT_SETTING}', :v, true)"), {"v": value})


class TestTheRoleMatters:
    async def test_the_default_role_bypasses_rls(self, engine: AsyncEngine) -> None:
        """The trap, asserted rather than described.

        The repository's own development role is a superuser, so every policy is inert against it.
        Pinning this means nobody can later read a green RLS test suite as evidence of isolation
        without also seeing that the default connection has none.
        """
        async with engine.connect() as conn:
            role, bypasses, reason = await role_bypasses_rls(conn)
        assert bypasses, f"expected the test role {role!r} to be exempt; the trap has moved"
        assert "exempt from every RLS policy" in reason

    async def test_the_probe_role_does_not(self, unprivileged_engine: AsyncEngine) -> None:
        async with unprivileged_engine.connect() as conn:
            _role, bypasses, _reason = await role_bypasses_rls(conn)
        assert not bypasses

    async def test_verify_enforced_reports_the_exemption_first(self, engine: AsyncEngine) -> None:
        # Listed first because it makes every other line in the report irrelevant.
        async with engine.connect() as conn:
            state = await verify_enforced(conn)
        assert state["problems"]
        assert state["problems"][0].startswith("RLS IS NOT IN EFFECT")


class TestIsolation:
    async def test_a_tenant_sees_only_its_own_rows(
        self, engine: AsyncEngine, unprivileged_engine: AsyncEngine
    ) -> None:
        alpha, _beta = await seed_two_tenants(engine)
        async with unprivileged_engine.connect() as conn:
            await with_tenant(conn, alpha)
            names = set((await conn.execute(text("SELECT name FROM traces"))).scalars().all())
        assert "alpha-run" in names
        assert "beta-run" not in names

    async def test_no_tenant_context_sees_nothing(
        self, engine: AsyncEngine, unprivileged_engine: AsyncEngine
    ) -> None:
        """Fails closed, which is the whole reason `current_setting(..., true)` takes the flag.

        Without the missing-ok flag the query would *raise*, and a code path that forgot to set the
        context would 500 rather than return an empty result. Both are safe; an empty result is the
        one that does not take the endpoint down.
        """
        await seed_two_tenants(engine)
        async with unprivileged_engine.connect() as conn:
            await with_tenant(conn, None)
            count = (await conn.execute(text("SELECT count(*) FROM traces"))).scalar_one()
        assert count == 0

    async def test_a_query_that_forgets_its_predicate_returns_nothing(
        self, engine: AsyncEngine, unprivileged_engine: AsyncEngine
    ) -> None:
        """The bug RLS exists to catch.

        A hand-written query with no `project_id` filter is exactly the layer-1 mistake the
        threat model names, and it is the one that would otherwise return another tenant's rows.
        """
        alpha, _beta = await seed_two_tenants(engine)
        async with unprivileged_engine.connect() as conn:
            await with_tenant(conn, alpha)
            # No tenant predicate anywhere in this statement.
            rows = (await conn.execute(text("SELECT project_id FROM traces"))).scalars().all()
        assert rows
        assert set(rows) == {alpha}

    async def test_writing_into_another_tenant_is_refused(
        self, engine: AsyncEngine, unprivileged_engine: AsyncEngine
    ) -> None:
        """`WITH CHECK`, not just `USING`.

        A policy with only `USING` filters reads and permits writes, so a caller can insert a row
        into another tenant that it then cannot see — a data-corruption bug wearing a security
        bug's clothes.
        """
        alpha, beta = await seed_two_tenants(engine)
        async with unprivileged_engine.connect() as conn:
            await with_tenant(conn, alpha)
            with pytest.raises(DBAPIError, match="row-level security"):
                await conn.execute(
                    text(
                        "INSERT INTO traces (id, project_id, trace_id, name, status, started_at) "
                        "VALUES (:id, :project, 'smuggled', 'smuggled', 'ok', now())"
                    ),
                    {"id": uuid.uuid4(), "project": beta},
                )

    async def test_updating_another_tenants_row_affects_nothing(
        self, engine: AsyncEngine, unprivileged_engine: AsyncEngine
    ) -> None:
        # An UPDATE cannot see the row, so it matches nothing rather than erroring — which is the
        # correct behaviour and is worth pinning separately from the INSERT case.
        alpha, beta = await seed_two_tenants(engine)
        async with unprivileged_engine.begin() as conn:
            await with_tenant(conn, alpha)
            result = await conn.execute(
                text("UPDATE traces SET name = 'tampered' WHERE project_id = :p"), {"p": beta}
            )
            assert result.rowcount == 0

        async with engine.connect() as owner:
            names = (
                (
                    await owner.execute(
                        text("SELECT name FROM traces WHERE project_id = :p"), {"p": beta}
                    )
                )
                .scalars()
                .all()
            )
        assert "tampered" not in names

    async def test_the_policy_reaches_partitions(
        self, engine: AsyncEngine, unprivileged_engine: AsyncEngine
    ) -> None:
        """Directly, not only through the parent.

        A partition attached before the parent's policy existed has no policy of its own, so a
        query naming the child would bypass it. The migration applies the DDL to existing children
        and `ensure_partitions` does the same for the ones it creates at startup — because those
        appear months after the migration ran.
        """
        alpha, _beta = await seed_two_tenants(engine)
        async with engine.connect() as owner:
            child = (
                await owner.execute(
                    text(
                        "SELECT c.relname FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid "
                        "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'traces' "
                        "AND c.relname <> 'traces_default' LIMIT 1"
                    )
                )
            ).scalar_one()

        async with unprivileged_engine.connect() as conn:
            await with_tenant(conn, alpha)
            rows = (await conn.execute(text(f"SELECT project_id FROM {child}"))).scalars().all()
        assert set(rows) <= {alpha}

    async def test_the_setting_does_not_survive_the_transaction(
        self, engine: AsyncEngine, unprivileged_engine: AsyncEngine
    ) -> None:
        """`SET LOCAL`, so a pooled connection cannot carry a tenant into the next request.

        This is the property that makes binding the tenant in a request dependency safe at all. A
        plain `SET` would persist for the connection's lifetime, and under a pool that is precisely
        the cross-tenant bug RLS is meant to catch.
        """
        alpha, _beta = await seed_two_tenants(engine)
        async with unprivileged_engine.begin() as conn:
            await with_tenant(conn, alpha)
            assert (await conn.execute(text("SELECT count(*) FROM traces"))).scalar_one() >= 1

        # A fresh transaction on the same engine (and very likely the same pooled connection).
        async with unprivileged_engine.begin() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM traces"))).scalar_one() == 0


class TestCoverage:
    async def test_every_tenant_scoped_table_is_protected_or_excused(self) -> None:
        """No tenant-scoped table may be silently absent from the policy list.

        Derived from the models rather than from the catalogue, so adding a table with a
        `project_id` and forgetting the policy fails here rather than in production.
        """
        import proofstep_api.db.models  # noqa: F401 — registers the mappers
        from proofstep_api.db.base import Base

        scoped = {
            name
            for name, table in Base.metadata.tables.items()
            if "project_id" in {column.name for column in table.columns}
        }
        missing = scoped - set(PROTECTED_TABLES) - set(UNPROTECTED_TABLES)
        assert not missing, (
            f"tenant-scoped tables with no RLS policy and no recorded exemption: {sorted(missing)}"
        )

    async def test_every_excused_table_has_a_reason(self) -> None:
        assert all(reason.strip() for reason in UNPROTECTED_TABLES.values())

    async def test_the_policies_are_installed(self, engine: AsyncEngine) -> None:
        async with engine.connect() as conn:
            state = await verify_enforced(conn)
        # Only the role exemption should be reported; every table must be enabled, forced, policied.
        table_problems = [p for p in state["problems"] if not p.startswith("RLS IS NOT IN EFFECT")]
        assert table_problems == []
        assert len(state["enforced"]) == len(PROTECTED_TABLES)
        assert state["unreviewed"] == []
