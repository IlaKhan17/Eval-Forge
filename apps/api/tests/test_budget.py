"""A monthly ceiling on server-initiated spend.

`max_cost` on a suite stops one run. Nothing stopped the sum of runs, and the spend that
accumulates without anyone starting it is the online-evaluation loop — a judge rule at a 1% sample
on a busy service bills continuously and quietly.

The properties worth pinning are the ones that decide whether the limit is trustworthy or merely
present: that it stops *paid* rules and not free ones, that a skip is recorded with a reason rather
than dropped, that it is scoped to the calendar month, and that unlimited is distinct from zero.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from evalforge_api.api.dependencies import get_session
from evalforge_api.db.models.evaluation import TrajectoryPolicyVersion
from evalforge_api.db.models.online import OnlineEvalRule, OnlineEvaluation
from evalforge_api.main import create_app
from evalforge_api.services import budget
from evalforge_api.services.online_eval import OnlineEvalService
from evalforge_api.settings import Settings
from factories import Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The online-eval helpers live beside the suite that introduced them rather than in factories.py,
# so this imports from there rather than duplicating three builders.
# The online-eval helpers live beside the suite that introduced them rather than in factories.py,
# so this imports from there rather than duplicating four builders.
from test_online_eval import make_evaluator_version, make_policy, make_rule, make_trace

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32"))

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


@pytest_asyncio.fixture
async def policy(session: AsyncSession, tenant_a: Tenant) -> TrajectoryPolicyVersion:
    return await make_policy(session, tenant_a)


async def spend(
    session: AsyncSession,
    tenant: Tenant,
    amount: str,
    *,
    when: datetime,
    rule: OnlineEvalRule,
) -> None:
    """Record spend directly, so a test can put a project over its limit without paying for it.

    Through a real rule, because an evaluation without one cannot exist — the column is NOT NULL,
    and a helper that invented a shape the schema refuses would prove nothing about the sum the
    budget actually reads.
    """
    session.add(
        OnlineEvaluation(
            project_id=tenant.project.id,
            trace_id=f"spend-{uuid.uuid4().hex[:8]}",
            rule_id=rule.id,
            verdict="pass",
            decision_reason="sampled",
            cost=Decimal(amount),
            created_at=when,
        )
    )
    await session.flush()


class TestStatus:
    async def test_no_limit_is_unlimited_not_zero(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # A project with no ceiling and one with a ceiling of 0 are opposite configurations, and
        # collapsing them would either stop all paid work or none of it.
        status = await budget.status(session, project_id=tenant_a.project.id)
        assert status.unlimited
        assert status.exhausted is False
        assert status.ratio is None

    async def test_a_zero_limit_is_immediately_exhausted(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # The real setting for a project that should run only its free deterministic rules.
        tenant_a.project.monthly_cost_limit = Decimal(0)
        await session.flush()

        status = await budget.status(session, project_id=tenant_a.project.id)
        assert status.unlimited is False
        assert status.exhausted is True

    async def test_only_this_month_counts(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        """Calendar month, because that is how the invoice arrives.

        Last month's spend counting against this month's ceiling would make a budget that never
        resets — and one nobody could reconcile against a bill.
        """
        tenant_a.project.monthly_cost_limit = Decimal("10.00")
        await session.flush()

        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        start = budget.month_start()
        await spend(session, tenant_a, "9.00", when=start - timedelta(days=1), rule=rule)
        await spend(session, tenant_a, "1.00", when=start + timedelta(minutes=1), rule=rule)

        status = await budget.status(session, project_id=tenant_a.project.id)
        assert status.spent == Decimal("1.00")
        assert status.exhausted is False

    async def test_it_warns_before_it_stops(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # Warning only at 100% is warning after the fact. The useful moment is while there is still
        # room to raise the limit or turn a rule down.
        tenant_a.project.monthly_cost_limit = Decimal("10.00")
        await session.flush()
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        await spend(
            session, tenant_a, "8.50", when=budget.month_start() + timedelta(minutes=1), rule=rule
        )

        status = await budget.status(session, project_id=tenant_a.project.id)
        assert status.warning is True
        assert status.exhausted is False

    async def test_spend_is_scoped_to_the_project(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        other_policy = await make_policy(session, tenant_b)
        rule = await make_rule(session, tenant_b, policy_version_id=other_policy.id)
        await spend(
            session, tenant_b, "50.00", when=budget.month_start() + timedelta(minutes=1), rule=rule
        )
        status = await budget.status(session, project_id=tenant_a.project.id)
        assert status.spent == Decimal(0)


class TestEnforcement:
    async def test_free_rules_keep_running_when_the_budget_is_gone(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        """The asymmetry the whole design rests on.

        A deterministic trajectory policy costs nothing per trace. Switching off the safety checks
        because the judge allowance ran out would trade a bill for an incident.
        """
        tenant_a.project.monthly_cost_limit = Decimal(0)
        await session.flush()
        for index in range(3):
            await make_trace(session, tenant_a, trace_id=f"free{index}")
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id, sample_rate=1.0)

        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[rule]
        )
        assert outcome.budget_exhausted is True
        assert outcome.evaluations_written == 3, "a free policy stopped running over a spend limit"
        assert outcome.reasons.get("budget") is None

    async def test_a_skipped_paid_rule_records_why(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        """A gap in coverage has to be visible as a reason, not as an absence.

        A month where nothing was judged and nothing says why is indistinguishable from a month
        where nothing needed judging, and the second is the story people tell themselves.
        """
        tenant_a.project.monthly_cost_limit = Decimal(0)
        await session.flush()
        await make_trace(session, tenant_a, trace_id="paid1")
        # A real judge rule: the schema refuses one without an evaluator version, and using a
        # trajectory rule with the check stubbed out would test the stub rather than the rule.
        rule = await make_rule(
            session,
            tenant_a,
            kind="llm_judge",
            evaluator_version_id=await make_evaluator_version(session, tenant_a),
            sample_rate=1.0,
            escalate=False,
        )

        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[rule]
        )
        assert outcome.skipped == 1
        assert outcome.reasons["budget"] == 1

        row = (
            await session.execute(
                select(OnlineEvaluation).where(OnlineEvaluation.trace_id == "paid1")
            )
        ).scalar_one()
        assert row.verdict == "skipped"
        assert row.decision_reason == "budget"

    async def test_a_paid_rule_runs_with_room_left(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        tenant_a.project.monthly_cost_limit = Decimal("100.00")
        await session.flush()
        await make_trace(session, tenant_a, trace_id="paid2")
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id, sample_rate=1.0)

        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[rule]
        )
        assert outcome.budget_exhausted is False
        assert outcome.reasons.get("budget") is None


class TestApi:
    async def test_reading_and_setting_the_ceiling(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        head = {"authorization": f"Bearer {tenant_a.token}"}

        initial = (await client.get("/v1/ops/budget", headers=head)).json()
        assert initial["monthly_limit"] is None
        assert initial["ratio"] is None
        # The scope is in the response, because a limit whose coverage is assumed is worse than one
        # whose coverage is written down.
        assert "online evaluation" in initial["covers"]

        updated = (
            await client.put("/v1/ops/budget", headers=head, json={"monthly_limit": 25})
        ).json()
        assert updated["monthly_limit"] == 25
        assert updated["remaining"] == 25

        cleared = (
            await client.put("/v1/ops/budget", headers=head, json={"monthly_limit": None})
        ).json()
        assert cleared["monthly_limit"] is None

    async def test_a_read_only_credential_cannot_raise_the_ceiling(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        # Raising a ceiling is how a bill gets bigger. A credential that can only read traces should
        # not be able to authorise that.
        from factories import make_tenant

        reader = await make_tenant(session, slug="reader-only", scopes=["read"])
        response = await client.put(
            "/v1/ops/budget",
            headers={"authorization": f"Bearer {reader.token}"},
            json={"monthly_limit": 1000},
        )
        assert response.status_code == 403

    async def test_the_ceiling_is_the_callers_own_project(
        self, client: AsyncClient, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        await client.put(
            "/v1/ops/budget",
            headers={"authorization": f"Bearer {tenant_a.token}"},
            json={"monthly_limit": 5},
        )
        other = (
            await client.get(
                "/v1/ops/budget", headers={"authorization": f"Bearer {tenant_b.token}"}
            )
        ).json()
        assert other["monthly_limit"] is None
