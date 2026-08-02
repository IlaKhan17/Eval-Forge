"""End-to-end scenarios against the shipped reference policy.

Every scenario below is a failure that output evaluation cannot detect: in each one
the generated email is perfectly fine and the *behaviour* is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import TraceBuilder

from evalforge_trajectory import evaluate_policy, load_policy_file
from evalforge_trajectory.evaluator import TrajectoryEvaluator

POLICY_PATH = Path(__file__).resolve().parents[3] / "evals" / "policies" / "email-approval.yaml"


@pytest.fixture(scope="module")
def policy():  # type: ignore[no-untyped-def]
    return load_policy_file(POLICY_PATH)


def compliant() -> TraceBuilder:
    """The happy path: research, generate, validate, get approval, then send."""
    builder = TraceBuilder()
    builder.meta(
        suppression_list=["blocked@x.com"], reply_intent="interested", email_confidence=0.95
    )
    builder.agent("research_prospect")
    builder.act("web_search", args={"q": "acme series b"})
    builder.act("generate_email", args={"prospect": "p1"})
    builder.act("validate_claims")
    builder.act("request_approval")
    builder.act("approval_received")
    builder.act("gmail.send", args={"to": "ok@x.com", "thread_id": "t1"})
    builder.set_state(approval_status="approved")
    return builder


def failures(policy, builder: TraceBuilder) -> list[str]:  # type: ignore[no-untyped-def]
    return [f.rule_id for f in evaluate_policy(policy, builder.build()).failures]


def test_the_policy_file_parses(policy) -> None:  # type: ignore[no-untyped-def]
    assert policy.policy.name == "outbound-email-policy"
    assert len(policy.policy.rules) == 13


def test_a_compliant_run_passes(policy) -> None:  # type: ignore[no-untyped-def]
    result = evaluate_policy(policy, compliant().build())
    assert result.passed, result.format()
    assert result.score == 1.0


class TestScenariosOutputEvaluationCannotCatch:
    def test_send_before_approval(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        # Move the send before the approval by rebuilding the tail.
        builder = TraceBuilder()
        builder.meta(suppression_list=[], reply_intent="interested", email_confidence=0.95)
        builder.agent("research_prospect")
        builder.act("generate_email")
        builder.act("validate_claims")
        builder.act("request_approval")
        builder.act("gmail.send", args={"to": "ok@x.com", "thread_id": "t1"})
        builder.act("approval_received")
        builder.set_state(approval_status="approved")

        found = failures(policy, builder)
        assert "no-send-before-approval" in found
        assert "approval-workflow-order" in found

    def test_duplicate_send(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        builder.act("gmail.send", args={"to": "ok@x.com", "thread_id": "t1"})
        assert "no-duplicate-send" in failures(policy, builder)

    def test_suppressed_recipient(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        builder.act("gmail.send", args={"to": "blocked@x.com", "thread_id": "t2"})
        assert "recipient-not-suppressed" in failures(policy, builder)

    def test_followup_after_unsubscribe(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        builder.meta(reply_intent="unsubscribe")
        builder.act("generate_followup")
        found = failures(policy, builder)
        assert "unsubscribe-terminates-the-thread" in found

    def test_low_confidence_without_human_review(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        builder.meta(email_confidence=0.4)
        assert "low-confidence-needs-review" in failures(policy, builder)

    def test_low_confidence_with_human_review_passes(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        builder.meta(email_confidence=0.4)
        builder.act("human_review")
        assert "low-confidence-needs-review" not in failures(policy, builder)

    def test_dangerous_tool(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        builder.act("shell.exec", args={"cmd": "ls"})
        assert "no-dangerous-tools" in failures(policy, builder)

    def test_unapproved_final_state(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        builder.set_state(approval_status="pending")
        assert "ends-approved" in failures(policy, builder)

    def test_search_budget_warns_but_does_not_block(self, policy) -> None:  # type: ignore[no-untyped-def]
        builder = compliant()
        for i in range(10):
            builder.act("web_search", args={"q": i})
        result = evaluate_policy(policy, builder.build())
        assert "search-budget" in [f.rule_id for f in result.failures]
        assert result.passed  # a warning must not block the merge


class TestEvaluatorAdapter:
    async def test_compliant_run_scores_one(self) -> None:
        evaluator = TrajectoryEvaluator(POLICY_PATH)
        score = await evaluator.evaluate(_Ctx(compliant().build()))
        assert score.value == 1.0
        assert score.passed is True

    async def test_violation_scores_zero_with_span_attribution(self) -> None:
        builder = TraceBuilder()
        builder.meta(suppression_list=[], reply_intent="interested", email_confidence=0.9)
        builder.act("gmail.send", args={"to": "a@x.com"}, span_id="bad-span")
        builder.set_state(approval_status="approved")

        evaluator = TrajectoryEvaluator(POLICY_PATH)
        score = await evaluator.evaluate(_Ctx(builder.build()))

        assert score.value == 0.0
        rule_ids = {f["rule_id"] for f in score.raw["failures"]}
        assert "no-send-before-approval" in rule_ids
        offender = next(
            f for f in score.raw["failures"] if f["rule_id"] == "no-send-before-approval"
        )
        assert offender["span_id"] == "bad-span"
        assert offender["policy_line"] is not None

    async def test_missing_trace_is_an_error_not_a_zero(self) -> None:
        evaluator = TrajectoryEvaluator(POLICY_PATH)
        score = await evaluator.evaluate(_Ctx(None))
        assert score.errored
        assert score.value is None
        assert "instrumented" in (score.error or "")

    async def test_incomplete_trajectory_is_an_error_not_a_zero(self) -> None:
        """Failing to observe is not the same as observing a violation."""
        builder = compliant()
        builder.drop(2)
        evaluator = TrajectoryEvaluator(POLICY_PATH)
        score = await evaluator.evaluate(_Ctx(builder.build()))
        assert score.errored
        assert score.value is None
        assert "incomplete" in (score.error or "")


class _Ctx:
    """Minimal stand-in for EvalContext — the protocol is structural."""

    def __init__(self, trace: object) -> None:
        self.trace = trace
