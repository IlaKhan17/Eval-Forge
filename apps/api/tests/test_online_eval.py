"""Online evaluation, review queues, and promotion, against a real Postgres.

These are integration tests on purpose. Three of the properties under test do not exist
outside a real database:

- `FOR UPDATE SKIP LOCKED` claiming, which needs two concurrent transactions
- `ON CONFLICT DO NOTHING` idempotency, which needs the actual unique constraints
- partition-aware retention, which needs actual partitions

A unit test with a fake session would assert that the code calls the right methods, not that
the behaviour is correct — and for concurrency, those are very different claims.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from evalforge_api.db.models.evaluation import (
    Dataset,
    DatasetVersion,
    Evaluator,
    EvaluatorVersion,
    TrajectoryPolicy,
    TrajectoryPolicyVersion,
)
from evalforge_api.db.models.identity import Organization
from evalforge_api.db.models.online import (
    Annotation,
    OnlineEvalRule,
    OnlineEvaluation,
    ReviewAssignment,
    ReviewQueue,
)
from evalforge_api.db.models.traces import Span as SpanRow
from evalforge_api.db.models.traces import Trace as TraceRow
from evalforge_api.errors import UnprocessableError
from evalforge_api.services.online_eval import OnlineEvalService, coverage, unprocessed_count
from evalforge_api.services.retention import droppable_partitions, month_end, month_start
from evalforge_api.services.review import ReviewService
from evalforge_api.worker import jobs
from factories import Tenant, make_tenant
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

# A policy that demands human approval before a send, which is the canonical thing an agent
# gets wrong in production.
POLICY_YAML = """
apiVersion: evalforge.dev/v1
kind: TrajectoryPolicy
name: approval-before-send
description: An email must not be sent before a human approves it.
include:
  span_types: [tool, agent]
rules:
  - id: approval-precedes-send
    kind: forbidden_before
    severity: block
    action: gmail.send
    before: human.approve
    message: an email was sent without human approval
"""


async def make_trace(
    session: AsyncSession,
    tenant: Tenant,
    *,
    trace_id: str,
    approved: bool = True,
    failed: bool = False,
    started_at: datetime | None = None,
    dropped: int = 0,
) -> TraceRow:
    """A two-span trace: optionally an approval, then a send."""
    moment = started_at or datetime.now(UTC) - timedelta(minutes=5)
    trace = TraceRow(
        project_id=tenant.project.id,
        trace_id=trace_id,
        name="reply-drafter",
        status="error" if failed else "ok",
        started_at=moment,
        ended_at=moment + timedelta(seconds=1),
        duration_ms=1000,
        span_count=2 if approved else 1,
        error_count=1 if failed else 0,
        dropped_span_count=dropped,
        capture_mode="redacted",
    )
    session.add(trace)

    sequence = 0
    if approved:
        session.add(
            SpanRow(
                project_id=tenant.project.id,
                trace_id=trace_id,
                span_id=f"{trace_id}-approve",
                name="approve",
                span_type="tool",
                tool_name="human.approve",
                status="ok",
                started_at=moment,
                ended_at=moment + timedelta(milliseconds=100),
                sequence_index=sequence,
            )
        )
        sequence += 1

    session.add(
        SpanRow(
            project_id=tenant.project.id,
            trace_id=trace_id,
            span_id=f"{trace_id}-send",
            name="send",
            span_type="tool",
            tool_name="gmail.send",
            status="error" if failed else "ok",
            started_at=moment + timedelta(milliseconds=200),
            ended_at=moment + timedelta(milliseconds=300),
            sequence_index=sequence,
            input_inline={"body": "hello"},
        )
    )
    await session.flush()
    return trace


#: A rule that asserts something *must* have happened. Over an incomplete trace this is
#: unanswerable — the missing spans might contain it — which is what makes it inconclusive
#: rather than a violation. `forbidden_before` is different: a send with no approval among the
#: spans we did see is a real violation whether or not other spans went missing.
REQUIRED_POLICY_YAML = """
apiVersion: evalforge.dev/v1
kind: TrajectoryPolicy
name: approval-required
description: Every run must record a human approval.
include:
  span_types: [tool, agent]
rules:
  - id: approval-happened
    kind: required_action
    severity: block
    action: human.approve
    message: no human approval was recorded
"""


async def make_policy(
    session: AsyncSession, tenant: Tenant, *, source: str = POLICY_YAML
) -> TrajectoryPolicyVersion:
    policy = TrajectoryPolicy(
        project_id=tenant.project.id,
        name="policy",
        slug=f"policy-{uuid.uuid4().hex[:6]}",
    )
    session.add(policy)
    await session.flush()
    version = TrajectoryPolicyVersion(
        project_id=tenant.project.id,
        policy_id=policy.id,
        version=1,
        source_yaml=source,
        parsed={},
        content_hash=uuid.uuid4().bytes + uuid.uuid4().bytes,
    )
    session.add(version)
    await session.flush()
    return version


async def make_queue(
    session: AsyncSession, tenant: Tenant, *, lease_seconds: int = 1800
) -> ReviewQueue:
    queue = ReviewQueue(
        project_id=tenant.project.id,
        name="Policy failures",
        slug=f"failures-{uuid.uuid4().hex[:6]}",
        lease_seconds=lease_seconds,
    )
    session.add(queue)
    await session.flush()
    return queue


async def make_evaluator_version(session: AsyncSession, tenant: Tenant) -> uuid.UUID:
    """A judge evaluator version, which a judge rule is required to name.

    The schema's CHECK constraint refuses a judge rule with no evaluator, and rightly: such a
    rule would look configured and evaluate nothing.
    """
    evaluator = Evaluator(
        project_id=tenant.project.id,
        name="tone",
        slug=f"tone-{uuid.uuid4().hex[:6]}",
        evaluator_type="llm_judge",
    )
    session.add(evaluator)
    await session.flush()
    version = EvaluatorVersion(
        project_id=tenant.project.id,
        evaluator_id=evaluator.id,
        version=1,
        config={"rubric": "x"},
        config_hash=uuid.uuid4().bytes + uuid.uuid4().bytes,
        judge_model="test-model",
    )
    session.add(version)
    await session.flush()
    return version.id


async def make_rule(
    session: AsyncSession,
    tenant: Tenant,
    *,
    kind: str = "trajectory",
    policy_version_id: uuid.UUID | None = None,
    queue_id: uuid.UUID | None = None,
    sample_rate: float = 0.01,
    escalate: bool = True,
    max_escalations: int = 50,
    evaluator_version_id: uuid.UUID | None = None,
) -> OnlineEvalRule:
    rule = OnlineEvalRule(
        project_id=tenant.project.id,
        name=f"rule-{kind}",
        slug=f"rule-{uuid.uuid4().hex[:6]}",
        kind=kind,
        policy_version_id=policy_version_id,
        evaluator_version_id=evaluator_version_id,
        review_queue_id=queue_id,
        sample_rate=sample_rate,
        escalate_on_failure=escalate,
        max_escalations_per_batch=max_escalations,
    )
    session.add(rule)
    await session.flush()
    return rule


@pytest_asyncio.fixture
async def policy(session: AsyncSession, tenant_a: Tenant) -> TrajectoryPolicyVersion:
    return await make_policy(session, tenant_a)


@pytest_asyncio.fixture
async def committed_tenant(engine: Any) -> AsyncIterator[tuple[Tenant, Any]]:
    """A tenant that is actually committed, plus a session factory.

    The shared `session` fixture isolates tests by rolling back, which means nothing it
    writes is visible on another connection — and the whole point of the SKIP LOCKED tests is
    two concurrent connections. So these tests get their own committed tenant with a unique
    slug, and delete it afterwards.

    Committing on the shared fixture instead would leave the org behind and every later test
    that creates a tenant with the same slug would fail on the unique index. That is exactly
    the failure this fixture exists to avoid.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    slug = f"concurrent-{uuid.uuid4().hex[:8]}"
    async with maker() as setup:
        tenant = await make_tenant(setup, slug=slug)
        await setup.commit()
        org_id = tenant.org.id

    try:
        yield tenant, maker
    finally:
        async with maker() as teardown:
            # A Core DELETE, so the schema's ON DELETE CASCADE does the work. An ORM
            # `session.delete(org)` instead tries to *nullify* projects.org_id, because the
            # relationship is not configured with passive_deletes — and that hits the
            # not-null constraint rather than cascading.
            await teardown.execute(sa_delete(Organization).where(Organization.id == org_id))
            await teardown.commit()


class TestDeterministicCoverage:
    async def test_a_trajectory_rule_evaluates_every_trace(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # The point of running policies online: they are free, so there is no reason to
        # sample them, and sampling them would lose coverage of exactly the safety
        # properties most worth having on every trace.
        for i in range(12):
            await make_trace(session, tenant_a, trace_id=f"cov{i:04d}")
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id, sample_rate=0.0)

        service = OnlineEvalService(session, project_id=tenant_a.project.id)
        outcome = await service.run_batch(rules=[rule])

        assert outcome.traces_considered == 12
        assert outcome.evaluations_written == 12
        assert outcome.skipped == 0
        assert outcome.reasons == {"deterministic": 12}

    async def test_a_passing_trace_is_recorded_as_a_pass(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        await make_trace(session, tenant_a, trace_id="good", approved=True)
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(rules=[rule])

        row = (
            await session.execute(
                select(OnlineEvaluation).where(OnlineEvaluation.trace_id == "good")
            )
        ).scalar_one()
        assert row.verdict == "pass"
        assert row.score == 1.0

    async def test_a_violating_trace_fails_and_names_the_rule(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        await make_trace(session, tenant_a, trace_id="unapproved", approved=False)
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(rules=[rule])

        row = (
            await session.execute(
                select(OnlineEvaluation).where(OnlineEvaluation.trace_id == "unapproved")
            )
        ).scalar_one()
        assert row.verdict == "fail"
        failures = row.detail["failures"]
        assert failures[0]["rule_id"] == "approval-precedes-send"
        assert "without human approval" in failures[0]["message"]

    async def test_an_incomplete_trace_is_inconclusive_not_a_failure(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # The distinction that matters most online. A trace whose spans were dropped cannot
        # answer a question about what did *not* happen. Calling that a violation would fill
        # the review queue with "your exporter dropped spans" items until people stopped
        # reading it; calling it a pass would hide a coverage gap behind a green number.
        await make_trace(session, tenant_a, trace_id="lossy", approved=False, dropped=3)
        queue = await make_queue(session, tenant_a)
        # A `required_action` rule, because that is the kind an incomplete trace cannot
        # answer: the approval might be among the spans that were dropped.
        required = await make_policy(session, tenant_a, source=REQUIRED_POLICY_YAML)
        rule = await make_rule(session, tenant_a, policy_version_id=required.id, queue_id=queue.id)
        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[rule]
        )

        row = (
            await session.execute(
                select(OnlineEvaluation).where(OnlineEvaluation.trace_id == "lossy")
            )
        ).scalar_one()
        assert row.verdict == "inconclusive"
        assert row.score is None
        assert outcome.failures == 0
        assert outcome.queued_for_review == 0
        assert "not a policy violation" in row.detail["note"]

    async def test_a_forbidden_rule_still_fires_on_an_incomplete_trace(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # The other half of the incomplete-trace rule, and the reason it is not simply
        # "incomplete means unknown". A send with no approval among the spans that *were*
        # recorded is a real violation; the missing spans cannot un-send the email.
        await make_trace(session, tenant_a, trace_id="lossy-send", approved=False, dropped=3)
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(rules=[rule])

        row = (
            await session.execute(
                select(OnlineEvaluation).where(OnlineEvaluation.trace_id == "lossy-send")
            )
        ).scalar_one()
        assert row.verdict == "fail"

    async def test_a_broken_rule_is_an_error_not_a_failing_trace(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # A malformed policy or a provider outage must not present as a quality regression,
        # and must not queue innocent traces for review.
        await make_trace(session, tenant_a, trace_id="fine")
        broken = TrajectoryPolicy(project_id=tenant_a.project.id, name="broken", slug="broken")
        session.add(broken)
        await session.flush()
        version = TrajectoryPolicyVersion(
            project_id=tenant_a.project.id,
            policy_id=broken.id,
            version=1,
            source_yaml=(
                "apiVersion: evalforge.dev/v1\nkind: TrajectoryPolicy\nname: broken\n"
                "rules:\n  - id: x\n    kind: not_a_real_kind\n"
            ),
            parsed={},
            content_hash=b"\x01" * 32,
        )
        session.add(version)
        await session.flush()

        queue = await make_queue(session, tenant_a)
        rule = await make_rule(session, tenant_a, policy_version_id=version.id, queue_id=queue.id)
        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[rule]
        )

        row = (
            await session.execute(
                select(OnlineEvaluation).where(OnlineEvaluation.trace_id == "fine")
            )
        ).scalar_one()
        assert row.verdict == "error"
        assert row.error
        assert outcome.errors == 1
        assert outcome.failures == 0
        assert outcome.queued_for_review == 0


class TestIdempotency:
    async def test_replaying_a_batch_writes_nothing_new(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # A worker that dies mid-batch and restarts must not double-count. An online metric
        # that drifts upward every replay is worse than no metric.
        for i in range(5):
            await make_trace(session, tenant_a, trace_id=f"idem{i}")
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        service = OnlineEvalService(session, project_id=tenant_a.project.id)

        first = await service.run_batch(rules=[rule])
        second = await service.run_batch(rules=[rule])

        assert first.evaluations_written == 5
        # Nothing left to consider: the pending query excludes traces already decided.
        assert second.traces_considered == 0
        total = (
            await session.execute(
                select(func.count())
                .select_from(OnlineEvaluation)
                .where(OnlineEvaluation.rule_id == rule.id)
            )
        ).scalar_one()
        assert total == 5

    async def test_a_late_arriving_trace_is_still_picked_up(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # Why the pending query is a NOT EXISTS rather than a timestamp high-water mark.
        # Ingestion is not ordered by `started_at` — a client can upload hours late — so a
        # cursor would skip every late arrival permanently.
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        service = OnlineEvalService(session, project_id=tenant_a.project.id)

        now = datetime.now(UTC)
        await make_trace(
            session, tenant_a, trace_id="recent", started_at=now - timedelta(minutes=1)
        )
        await service.run_batch(rules=[rule], now=now)

        # This trace *started* earlier than the one already processed, but arrived after.
        await make_trace(session, tenant_a, trace_id="late", started_at=now - timedelta(hours=3))
        second = await service.run_batch(rules=[rule], now=now)

        assert second.traces_considered == 1
        assert second.evaluations_written == 1

    async def test_two_rules_failing_on_one_trace_queue_it_once(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # Otherwise two reviewers do the same work on the same trace.
        await make_trace(session, tenant_a, trace_id="double", approved=False)
        queue = await make_queue(session, tenant_a)
        first = await make_rule(session, tenant_a, policy_version_id=policy.id, queue_id=queue.id)
        second = await make_rule(session, tenant_a, policy_version_id=policy.id, queue_id=queue.id)

        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[first, second]
        )
        assert outcome.failures == 2
        assert outcome.queued_for_review == 1


class TestSamplingAndEscalation:
    async def test_a_judge_rule_records_a_skip_with_its_reason(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # A skip is a row, not a silence. Otherwise "no score" cannot be told apart from
        # "not sampled", "budget exhausted", or "the worker never got here".
        for i in range(6):
            await make_trace(session, tenant_a, trace_id=f"skip{i}")
        rule = await make_rule(
            session,
            tenant_a,
            kind="llm_judge",
            evaluator_version_id=await make_evaluator_version(session, tenant_a),
            sample_rate=0.0,
            escalate=False,
        )
        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[rule]
        )

        assert outcome.skipped == 6
        assert outcome.reasons == {"not_sampled": 6}
        rows = (
            (
                await session.execute(
                    select(OnlineEvaluation).where(OnlineEvaluation.rule_id == rule.id)
                )
            )
            .scalars()
            .all()
        )
        assert all(row.verdict == "skipped" for row in rows)
        assert all(row.decision_reason == "not_sampled" for row in rows)

    async def test_a_failed_trace_escalates_and_the_budget_caps_it(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # An incident produces an error spike. Without the cap that becomes a judge-call
        # spike and a surprise bill on the worst possible day.
        for i in range(10):
            await make_trace(session, tenant_a, trace_id=f"boom{i}", failed=True)
        rule = await make_rule(
            session,
            tenant_a,
            kind="llm_judge",
            evaluator_version_id=await make_evaluator_version(session, tenant_a),
            sample_rate=0.0,
            max_escalations=3,
        )
        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[rule]
        )

        assert outcome.reasons.get("escalated") == 3
        assert outcome.reasons.get("capped") == 7
        # Escalated traces reached the (unimplemented) judge and were recorded as errors
        # rather than silently passing.
        errors = (
            await session.execute(
                select(func.count())
                .select_from(OnlineEvaluation)
                .where(OnlineEvaluation.rule_id == rule.id, OnlineEvaluation.verdict == "error")
            )
        ).scalar_one()
        assert errors == 3

    async def test_coverage_distinguishes_unprocessed_from_unsampled(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # The number that makes online evaluation auditable. Both states produce the same
        # pass rate and only one of them means the worker is behind.
        for i in range(4):
            await make_trace(session, tenant_a, trace_id=f"seen{i}")
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(rules=[rule])

        for i in range(3):
            await make_trace(session, tenant_a, trace_id=f"unseen{i}")

        since = datetime.now(UTC) - timedelta(hours=1)
        by_reason = await coverage(
            session, project_id=tenant_a.project.id, rule_id=rule.id, since=since
        )
        backlog = await unprocessed_count(
            session, project_id=tenant_a.project.id, rule_id=rule.id, since=since
        )
        assert by_reason == {"deterministic": 4}
        assert backlog == 3


class TestReviewQueue:
    async def test_a_failure_reaches_the_queue_with_a_reason(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        await make_trace(session, tenant_a, trace_id="needs-review", approved=False)
        queue = await make_queue(session, tenant_a)
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id, queue_id=queue.id)
        await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(rules=[rule])

        assignment = (
            await session.execute(
                select(ReviewAssignment).where(ReviewAssignment.queue_id == queue.id)
            )
        ).scalar_one()
        assert assignment.target_id == "needs-review"
        assert assignment.status == "pending"
        # A reviewer handed a trace with no reason to look at it will not look at it.
        assert assignment.reason is not None
        assert "approval" in assignment.reason

    async def test_claiming_marks_it_in_review_with_a_lease(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        queue = await make_queue(session, tenant_a, lease_seconds=60)
        service = ReviewService(session, project_id=tenant_a.project.id)
        await service.enqueue(queue_id=queue.id, target_type="trace", target_id="t1")

        claimed = await service.claim_next(queue_id=queue.id, reviewer_id=tenant_a.user.id)
        assert claimed is not None
        assert claimed.assignment.status == "in_review"
        assert claimed.assignment.assignee_id == tenant_a.user.id
        assert claimed.assignment.lease_expires_at is not None

    async def test_priority_then_age_decides_the_order(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        queue = await make_queue(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)
        await service.enqueue(queue_id=queue.id, target_type="trace", target_id="old", priority=0)
        await service.enqueue(
            queue_id=queue.id, target_type="trace", target_id="urgent", priority=9
        )

        first = await service.claim_next(queue_id=queue.id, reviewer_id=None)
        second = await service.claim_next(queue_id=queue.id, reviewer_id=None)
        assert first is not None
        assert second is not None
        assert first.assignment.target_id == "urgent"
        # Oldest next rather than newest, so a backlog drains instead of growing a tail
        # nobody ever reaches.
        assert second.assignment.target_id == "old"

    async def test_an_empty_queue_returns_nothing(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        queue = await make_queue(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)
        assert await service.claim_next(queue_id=queue.id, reviewer_id=None) is None

    async def test_an_expired_lease_returns_the_item_to_the_pool(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # A reviewer who claims an item and closes their laptop must not hold it forever.
        queue = await make_queue(session, tenant_a, lease_seconds=60)
        service = ReviewService(session, project_id=tenant_a.project.id)
        await service.enqueue(queue_id=queue.id, target_type="trace", target_id="abandoned")

        start = datetime.now(UTC)
        claimed = await service.claim_next(
            queue_id=queue.id, reviewer_id=tenant_a.user.id, now=start
        )
        assert claimed is not None
        # Nothing available while the lease holds.
        assert await service.claim_next(queue_id=queue.id, reviewer_id=None, now=start) is None

        later = start + timedelta(seconds=120)
        again = await service.claim_next(queue_id=queue.id, reviewer_id=None, now=later)
        assert again is not None
        assert again.assignment.target_id == "abandoned"

    async def test_completing_is_idempotent(self, session: AsyncSession, tenant_a: Tenant) -> None:
        # A double-submitted form should not look like a failure to whoever submitted it.
        queue = await make_queue(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)
        assignment = await service.enqueue(
            queue_id=queue.id, target_type="trace", target_id="done-twice"
        )
        assert assignment is not None

        first = await service.complete(assignment.id)
        second = await service.complete(assignment.id)
        assert first.status == "done"
        assert second.completed_at == first.completed_at

    async def test_a_reviewer_cannot_invent_a_status(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        queue = await make_queue(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)
        assignment = await service.enqueue(queue_id=queue.id, target_type="trace", target_id="x")
        assert assignment is not None
        with pytest.raises(UnprocessableError):
            await service.complete(assignment.id, status="pending")

    async def test_another_tenants_queue_is_not_found(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        queue = await make_queue(session, tenant_a)
        other = ReviewService(session, project_id=tenant_b.project.id)
        from evalforge_api.errors import NotFoundError

        # 404, never 403: a 403 confirms the queue exists.
        with pytest.raises(NotFoundError):
            await other.claim_next(queue_id=queue.id, reviewer_id=None)


class TestConcurrentClaiming:
    """Why `FOR UPDATE SKIP LOCKED` is there.

    These use genuinely concurrent transactions on separate connections, not two sequential
    calls. Without SKIP LOCKED the second reviewer blocks on the first's row lock and the UI
    appears to hang; without FOR UPDATE both claim the same row and two people review the
    same trace.
    """

    @staticmethod
    async def _claim(maker: Any, project_id: uuid.UUID, queue_id: uuid.UUID) -> str | None:
        async with maker() as other:
            claimed = await ReviewService(other, project_id=project_id).claim_next(
                queue_id=queue_id, reviewer_id=None
            )
            await other.commit()
            return claimed.assignment.target_id if claimed else None

    async def test_two_reviewers_get_different_items(
        self, committed_tenant: tuple[Tenant, Any]
    ) -> None:
        tenant, maker = committed_tenant
        async with maker() as setup:
            queue = await make_queue(setup, tenant)
            service = ReviewService(setup, project_id=tenant.project.id)
            for i in range(2):
                await service.enqueue(queue_id=queue.id, target_type="trace", target_id=f"c{i}")
            await setup.commit()

        first, second = await asyncio.gather(
            self._claim(maker, tenant.project.id, queue.id),
            self._claim(maker, tenant.project.id, queue.id),
        )
        assert {first, second} == {"c0", "c1"}

    async def test_more_reviewers_than_items_is_safe(
        self, committed_tenant: tuple[Tenant, Any]
    ) -> None:
        tenant, maker = committed_tenant
        async with maker() as setup:
            queue = await make_queue(setup, tenant)
            await ReviewService(setup, project_id=tenant.project.id).enqueue(
                queue_id=queue.id, target_type="trace", target_id="only-one"
            )
            await setup.commit()

        results = await asyncio.gather(
            *[self._claim(maker, tenant.project.id, queue.id) for _ in range(4)]
        )
        # Exactly one winner; the losers get None rather than an error or a duplicate.
        assert [r for r in results if r] == ["only-one"]


class TestPromotion:
    async def make_dataset(self, session: AsyncSession, tenant: Tenant) -> Dataset:
        dataset = Dataset(project_id=tenant.project.id, name="Golden", slug="golden", kind="golden")
        session.add(dataset)
        await session.flush()
        return dataset

    async def test_promoting_creates_a_draft_and_an_example(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await make_trace(session, tenant_a, trace_id="promote-me", approved=False)
        await self.make_dataset(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)

        outcome = await service.promote_trace(
            trace_id="promote-me",
            dataset_slug="golden",
            expected={"approved_first": True},
        )
        assert outcome.created_draft
        assert not outcome.already_present

        version = await session.get(DatasetVersion, outcome.dataset_version_id)
        assert version is not None
        assert version.status == "draft"
        assert version.example_count == 1

    async def test_the_example_carries_the_span_input_and_its_provenance(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # An example nobody can trace back to a real interaction is an example nobody can
        # judge when it later looks wrong.
        await make_trace(session, tenant_a, trace_id="prov", approved=False)
        await self.make_dataset(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)
        outcome = await service.promote_trace(
            trace_id="prov", dataset_slug="golden", expected={"ok": True}
        )

        from evalforge_api.db.models.evaluation import DatasetExample

        example = (
            await session.execute(
                select(DatasetExample).where(
                    DatasetExample.dataset_version_id == outcome.dataset_version_id
                )
            )
        ).scalar_one()
        assert example.source_trace_id == "prov"
        assert example.input == {"body": "hello"}
        assert example.example_metadata["promoted_from"] == "trace"

    async def test_promotion_never_appends_to_a_locked_version(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # The load-bearing safety property. A locked version's content hash is what lets an
        # experiment prove it saw identical data; appending to one would silently invalidate
        # every historical comparison against it.
        await make_trace(session, tenant_a, trace_id="safe", approved=False)
        dataset = await self.make_dataset(session, tenant_a)
        locked = DatasetVersion(
            project_id=tenant_a.project.id,
            dataset_id=dataset.id,
            version="v1",
            status="locked",
            content_hash=b"\x02" * 32,
            locked_at=datetime.now(UTC),
            example_count=0,
        )
        session.add(locked)
        await session.flush()

        service = ReviewService(session, project_id=tenant_a.project.id)
        outcome = await service.promote_trace(
            trace_id="safe", dataset_slug="golden", expected={"ok": True}
        )

        assert outcome.dataset_version_id != locked.id
        assert outcome.created_draft
        refreshed = await session.get(DatasetVersion, locked.id)
        assert refreshed is not None
        assert refreshed.example_count == 0
        assert refreshed.content_hash == b"\x02" * 32

    async def test_promoting_twice_is_idempotent(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # Duplicates would skew every metric computed over the dataset.
        await make_trace(session, tenant_a, trace_id="twice", approved=False)
        await self.make_dataset(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)

        first = await service.promote_trace(
            trace_id="twice", dataset_slug="golden", expected={"ok": True}
        )
        second = await service.promote_trace(
            trace_id="twice", dataset_slug="golden", expected={"ok": True}
        )
        assert not first.already_present
        assert second.already_present
        assert second.dataset_version_id == first.dataset_version_id

        version = await session.get(DatasetVersion, first.dataset_version_id)
        assert version is not None
        assert version.example_count == 1

    async def test_promotion_refuses_without_a_human_expected_result(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # Promoting with the model's own output as the expected answer would enshrine the
        # defect as the specification — the failure that makes a golden dataset harmful.
        await make_trace(session, tenant_a, trace_id="no-answer", approved=False)
        await self.make_dataset(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)

        with pytest.raises(UnprocessableError, match="expected result from a human"):
            await service.promote_trace(trace_id="no-answer", dataset_slug="golden")

    async def test_an_annotation_correction_supplies_the_expected_result(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await make_trace(session, tenant_a, trace_id="annotated", approved=False)
        await self.make_dataset(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)

        annotation = await service.annotate(
            target_type="trace",
            target_id="annotated",
            annotator_id=tenant_a.user.id,
            comment="should have waited for approval",
            correction={"requires_approval": True},
        )
        outcome = await service.promote_trace(
            trace_id="annotated", dataset_slug="golden", annotation_id=annotation.id
        )

        from evalforge_api.db.models.evaluation import DatasetExample

        example = (
            await session.execute(
                select(DatasetExample).where(
                    DatasetExample.dataset_version_id == outcome.dataset_version_id
                )
            )
        ).scalar_one()
        assert example.expected == {"requires_approval": True}


class TestAnnotations:
    async def test_an_empty_annotation_is_refused(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # "I looked and had no opinion" must not enter the ground-truth table, where it would
        # count as a label.
        service = ReviewService(session, project_id=tenant_a.project.id)
        with pytest.raises(UnprocessableError, match="needs a label"):
            await service.annotate(
                target_type="trace", target_id="t", annotator_id=tenant_a.user.id
            )

    async def test_annotations_are_listed_in_order(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        service = ReviewService(session, project_id=tenant_a.project.id)
        await service.annotate(
            target_type="trace", target_id="t", annotator_id=tenant_a.user.id, label="bad"
        )
        await service.annotate(
            target_type="trace", target_id="t", annotator_id=tenant_a.user.id, rating=2.0
        )
        rows = await service.annotations_for(target_type="trace", target_id="t")
        assert [r.label for r in rows] == ["bad", None]

    async def test_a_preference_needs_a_counterpart(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # A pairwise preference with nothing to compare against is not a judgement.
        from sqlalchemy.exc import IntegrityError

        session.add(
            Annotation(
                project_id=tenant_a.project.id,
                target_type="trace",
                target_id="t",
                preference_winner="a",
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


class TestJobs:
    async def test_the_online_eval_job_reports_what_it_did(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        await make_trace(session, tenant_a, trace_id="job1", approved=False)
        await make_trace(session, tenant_a, trace_id="job2", approved=True)
        queue = await make_queue(session, tenant_a)
        await make_rule(session, tenant_a, policy_version_id=policy.id, queue_id=queue.id)

        report = await jobs.run_online_eval(session, project_ids=[tenant_a.project.id])
        assert report.written == 2
        assert report.failures == 1
        assert report.queued_for_review == 1

    async def test_the_rollup_reports_coverage_not_just_a_pass_rate(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # A pass rate over evaluated traces answers a different question from one over all
        # traces. Reporting only the numerator invites the wrong reading.
        await make_trace(session, tenant_a, trace_id="r1", approved=True)
        await make_trace(session, tenant_a, trace_id="r2", approved=False)
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(rules=[rule])

        report = await jobs.rollup_online_metrics(session, project_ids=[tenant_a.project.id])
        stats = report.detail[str(tenant_a.project.id)][str(rule.id)]
        assert stats["pass"] == 1
        assert stats["fail"] == 1
        assert stats["pass_rate"] == pytest.approx(0.5)
        assert stats["coverage"] == pytest.approx(1.0)

    async def test_a_rollup_over_nothing_reports_none_not_zero(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # A pass rate of 0.0 over zero measurements would show on a dashboard as a total
        # collapse.
        rule = await make_rule(
            session,
            tenant_a,
            kind="llm_judge",
            evaluator_version_id=await make_evaluator_version(session, tenant_a),
            sample_rate=0.0,
            escalate=False,
        )
        await make_trace(session, tenant_a, trace_id="none1")
        await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(rules=[rule])

        report = await jobs.rollup_online_metrics(session, project_ids=[tenant_a.project.id])
        stats = report.detail[str(tenant_a.project.id)][str(rule.id)]
        assert stats["evaluated"] == 0
        assert stats["pass_rate"] is None
        assert stats["skipped"] == 1

    async def test_the_lease_job_releases_abandoned_claims(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        queue = await make_queue(session, tenant_a, lease_seconds=1)
        service = ReviewService(session, project_id=tenant_a.project.id)
        await service.enqueue(queue_id=queue.id, target_type="trace", target_id="stuck")
        start = datetime.now(UTC)
        await service.claim_next(queue_id=queue.id, reviewer_id=tenant_a.user.id, now=start)

        report = await jobs.release_expired_leases(
            session, project_ids=[tenant_a.project.id], now=start + timedelta(seconds=10)
        )
        assert report.released == 1

    async def test_queue_health_reports_the_age_of_the_oldest_item(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # Age matters more than depth: five items where the oldest is three weeks old means
        # nobody is reading the queue.
        queue = await make_queue(session, tenant_a)
        service = ReviewService(session, project_id=tenant_a.project.id)
        await service.enqueue(queue_id=queue.id, target_type="trace", target_id="waiting")

        health = await jobs.queue_health(session, project_id=tenant_a.project.id)
        assert health[queue.slug]["pending"] == 1
        assert health[queue.slug]["oldest_pending"] is not None

    async def test_retention_deletes_payload_rows_past_their_window(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        from evalforge_api.db.models.traces import PayloadObject
        from evalforge_api.services.retention import RetentionService

        old = PayloadObject(
            project_id=tenant_a.project.id,
            sha256=b"\x03" * 32,
            bucket="evalforge-test",
            object_key=f"{tenant_a.project.id}/old",
            size_bytes=10,
            content_type="application/json",
            created_at=datetime.now(UTC) - timedelta(days=90),
        )
        fresh = PayloadObject(
            project_id=tenant_a.project.id,
            sha256=b"\x04" * 32,
            bucket="evalforge-test",
            object_key=f"{tenant_a.project.id}/fresh",
            size_bytes=10,
            content_type="application/json",
        )
        session.add_all([old, fresh])
        await session.flush()

        deleted = await RetentionService(session).sweep_payload_rows(
            project_id=tenant_a.project.id, days=14
        )
        assert deleted == 1
        assert await session.get(PayloadObject, fresh.id) is not None


class TestPartitionBoundary:
    """Pure tests: the boundary arithmetic is where a data-loss bug would live."""

    def test_a_partition_is_kept_until_its_whole_range_expires(self) -> None:
        # Comparing the partition's *start* against the cutoff would drop the current month
        # on the first day of retention and delete data the project asked to keep.
        cutoff = datetime(2026, 3, 15, tzinfo=UTC)
        drop, keep = droppable_partitions(
            ["traces_2026_01", "traces_2026_02", "traces_2026_03"],
            cutoff=cutoff,
            tables=("traces",),
        )
        assert drop == ["traces_2026_01", "traces_2026_02"]
        # March still holds rows newer than the cutoff.
        assert "traces_2026_03" in keep

    def test_a_partition_ending_exactly_on_the_cutoff_is_dropped(self) -> None:
        drop, _ = droppable_partitions(
            ["traces_2026_02"], cutoff=datetime(2026, 3, 1, tzinfo=UTC), tables=("traces",)
        )
        assert drop == ["traces_2026_02"]

    def test_the_default_partition_is_never_dropped(self) -> None:
        # Rows in it have no known range, so nothing can be proven about their age.
        drop, keep = droppable_partitions(
            ["traces_default"], cutoff=datetime(2030, 1, 1, tzinfo=UTC), tables=("traces",)
        )
        assert drop == []
        assert keep == ["traces_default"]

    def test_an_unrelated_table_is_left_alone(self) -> None:
        drop, keep = droppable_partitions(
            ["something_2020_01"], cutoff=datetime(2030, 1, 1, tzinfo=UTC), tables=("traces",)
        )
        assert drop == []
        assert keep == ["something_2020_01"]

    def test_december_rolls_over_to_january(self) -> None:
        assert month_end(datetime(2026, 12, 1, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=UTC)

    def test_a_malformed_name_is_not_a_month(self) -> None:
        assert month_start("traces_2026_13") is None
        assert month_start("traces") is None


class TestCostAccounting:
    async def test_a_deterministic_batch_costs_nothing(
        self, session: AsyncSession, tenant_a: Tenant, policy: TrajectoryPolicyVersion
    ) -> None:
        # If this ever stops being true, every claim about running policies on 100% of
        # traffic stops being affordable.
        for i in range(20):
            await make_trace(session, tenant_a, trace_id=f"free{i}")
        rule = await make_rule(session, tenant_a, policy_version_id=policy.id)
        outcome = await OnlineEvalService(session, project_id=tenant_a.project.id).run_batch(
            rules=[rule]
        )
        assert outcome.cost == Decimal(0)
