"""One positive and one negative case for each of the twelve rule kinds."""

from __future__ import annotations

from builders import TraceBuilder, policy_yaml

from proofstep_trajectory import evaluate_policy, load_policy
from proofstep_types import Status


def run(trace: TraceBuilder, *rules: str, extra: str = "") -> list[str]:
    """Return the ids of rules that failed."""
    loaded = load_policy(policy_yaml(*rules, extra=extra))
    return [f.rule_id for f in evaluate_policy(loaded, trace.build()).failures]


ORDER = """
- id: order
  kind: required_order
  steps: [research, generate, approve, send]
"""


class TestRequiredOrder:
    def test_correct_order_passes(self, trace: TraceBuilder) -> None:
        for action in ("research", "generate", "approve", "send"):
            trace.act(action)
        assert run(trace, ORDER) == []

    def test_intervening_actions_are_allowed_by_default(self, trace: TraceBuilder) -> None:
        for action in ("research", "web_search", "generate", "log", "approve", "send"):
            trace.act(action)
        assert run(trace, ORDER) == []

    def test_wrong_order_fails(self, trace: TraceBuilder) -> None:
        for action in ("research", "generate", "send", "approve"):
            trace.act(action)
        assert run(trace, ORDER) == ["order"]

    def test_failure_names_the_unmatched_step(self, trace: TraceBuilder) -> None:
        for action in ("research", "generate"):
            trace.act(action)
        loaded = load_policy(policy_yaml(ORDER))
        failure = evaluate_policy(loaded, trace.build()).failures[0]
        assert "'approve'" in failure.message
        assert "matched 2/4 steps" in str(failure.actual)

    def test_contiguous_mode_rejects_interleaving(self, trace: TraceBuilder) -> None:
        for action in ("research", "web_search", "generate", "approve", "send"):
            trace.act(action)
        rule = ORDER.replace("steps:", "mode: contiguous\n  steps:")
        assert run(trace, rule) == ["order"]


class TestRequiredAction:
    def test_present_passes(self, trace: TraceBuilder) -> None:
        trace.act("validate")
        assert run(trace, "- id: req\n  kind: required_action\n  action: validate\n") == []

    def test_absent_fails(self, trace: TraceBuilder) -> None:
        trace.act("other")
        assert run(trace, "- id: req\n  kind: required_action\n  action: validate\n") == ["req"]

    def test_min_count(self, trace: TraceBuilder) -> None:
        trace.act("check")
        rule = "- id: req\n  kind: required_action\n  action: check\n  min_count: 2\n"
        assert run(trace, rule) == ["req"]


FORBIDDEN = "- id: forbid\n  kind: forbidden_action\n  actions: [shell.exec, db.raw_query]\n"


class TestForbiddenAction:
    def test_absent_passes(self, trace: TraceBuilder) -> None:
        trace.act("safe")
        assert run(trace, FORBIDDEN) == []

    def test_present_fails(self, trace: TraceBuilder) -> None:
        trace.act("shell.exec")
        assert run(trace, FORBIDDEN) == ["forbid"]

    def test_a_failed_attempt_still_counts(self, trace: TraceBuilder) -> None:
        """Trying to run a forbidden tool is a violation even if it errored."""
        trace.act("shell.exec", status=Status.ERROR)
        assert run(trace, FORBIDDEN) == ["forbid"]

    def test_ignore_failed_opts_out(self, trace: TraceBuilder) -> None:
        trace.act("shell.exec", status=Status.ERROR)
        assert run(trace, FORBIDDEN.rstrip() + "\n  ignore_failed: true\n") == []


APPROVAL = "- id: approval\n  kind: forbidden_before\n  action: send\n  before: approved\n"


class TestForbiddenBefore:
    def test_correct_order_passes(self, trace: TraceBuilder) -> None:
        trace.act("approved")
        trace.act("send")
        assert run(trace, APPROVAL) == []

    def test_send_before_approval_fails(self, trace: TraceBuilder) -> None:
        trace.act("send")
        trace.act("approved")
        assert run(trace, APPROVAL) == ["approval"]

    def test_approval_never_happening_fails_when_the_action_occurred(
        self, trace: TraceBuilder
    ) -> None:
        trace.act("send")
        assert run(trace, APPROVAL) == ["approval"]

    def test_vacuous_when_the_action_never_occurs(self, trace: TraceBuilder) -> None:
        """The most common author surprise, made explicit."""
        trace.act("something_else")
        assert run(trace, APPROVAL) == []

    def test_concurrent_events_are_not_treated_as_ordered(self, trace: TraceBuilder) -> None:
        """A race is not proof of a violation.

        False-positive policy failures destroy trust in the gate faster than false
        negatives do, so overlapping events are deliberately not ordered.
        """
        parent = trace.agent("fanout")
        trace.parallel("send", "approved", parent=parent)
        assert run(trace, APPROVAL) == []


AFTER = "- id: fa\n  kind: forbidden_after\n  action: edit\n  after: finalize\n"


class TestForbiddenAfter:
    def test_before_the_gate_passes(self, trace: TraceBuilder) -> None:
        trace.act("edit")
        trace.act("finalize")
        assert (
            run(trace, "- id: fa\n  kind: forbidden_after\n  action: edit\n  after: finalize\n")
            == []
        )

    def test_after_the_gate_fails(self, trace: TraceBuilder) -> None:
        trace.act("finalize")
        trace.act("edit")
        assert run(
            trace, "- id: fa\n  kind: forbidden_after\n  action: edit\n  after: finalize\n"
        ) == ["fa"]


LIMIT = "- id: budget\n  kind: limit\n  action: web_search\n  max_calls: 3\n"


class TestLimit:
    def test_within_budget_passes(self, trace: TraceBuilder) -> None:
        for _ in range(3):
            trace.act("web_search", args={"q": "x"})
        assert run(trace, LIMIT) == []

    def test_over_budget_fails(self, trace: TraceBuilder) -> None:
        for i in range(5):
            trace.act("web_search", args={"q": i})
        assert run(trace, LIMIT) == ["budget"]

    def test_retries_do_not_count_toward_a_call_budget(self, trace: TraceBuilder) -> None:
        """A flaky network is not overuse of a tool."""
        for _ in range(5):
            trace.act("web_search", args={"q": "same"}, status=Status.ERROR)
        trace.act("web_search", args={"q": "same"})
        assert run(trace, LIMIT) == []

    def test_min_calls(self, trace: TraceBuilder) -> None:
        rule = "- id: budget\n  kind: limit\n  action: web_search\n  min_calls: 2\n"
        trace.act("web_search", args={"q": "x"})
        assert run(trace, rule) == ["budget"]


DUPLICATE = "- id: dup\n  kind: unique_action\n  action: send\n  key: [args.to]\n"


class TestUniqueAction:
    def test_distinct_recipients_pass(self, trace: TraceBuilder) -> None:
        trace.act("send", args={"to": "a@x.com"})
        trace.act("send", args={"to": "b@x.com"})
        assert run(trace, DUPLICATE) == []

    def test_same_recipient_twice_fails(self, trace: TraceBuilder) -> None:
        trace.act("send", args={"to": "a@x.com"})
        trace.act("send", args={"to": "a@x.com"})
        assert run(trace, DUPLICATE) == ["dup"]

    def test_a_retried_side_effect_still_counts_as_a_duplicate(self, trace: TraceBuilder) -> None:
        """Two actual sends sent two actual emails, whatever the agent intended."""
        trace.act("send", args={"to": "a@x.com"}, status=Status.ERROR)
        trace.act("send", args={"to": "a@x.com"})
        assert run(trace, DUPLICATE) == ["dup"]


LOOP = "- id: loop\n  kind: no_loop\n  window: 6\n  min_repeats: 3\n"


class TestNoLoop:
    def test_varied_actions_pass(self, trace: TraceBuilder) -> None:
        for i in range(6):
            trace.act("search", args={"q": i})
        assert run(trace, LOOP) == []

    def test_repetition_fails(self, trace: TraceBuilder) -> None:
        for _ in range(4):
            trace.act("search", args={"q": "same"})
        assert run(trace, "- id: loop\n  kind: no_loop\n  window: 6\n  min_repeats: 3\n") == [
            "loop"
        ]


class TestArgumentCondition:
    def test_allowed_recipient_passes(self, trace: TraceBuilder) -> None:
        trace.meta(suppression_list=["blocked@x.com"])
        trace.act("send", args={"to": "ok@x.com"})
        rule = (
            "- id: suppress\n  kind: argument_condition\n  action: send\n"
            "  require: args.to not in metadata.suppression_list\n"
        )
        assert run(trace, rule) == []

    def test_suppressed_recipient_fails(self, trace: TraceBuilder) -> None:
        trace.meta(suppression_list=["blocked@x.com"])
        trace.act("send", args={"to": "blocked@x.com"})
        rule = (
            "- id: suppress\n  kind: argument_condition\n  action: send\n"
            "  require: args.to not in metadata.suppression_list\n"
        )
        assert run(trace, rule) == ["suppress"]


CONDITIONAL = (
    "- id: unsub\n  kind: conditional\n"
    "  when: metadata.reply_intent == 'unsubscribe'\n"
    "  forbid_actions: [send, generate_followup]\n"
)


class TestConditional:
    def test_condition_not_met_skips_the_rule(self, trace: TraceBuilder) -> None:
        trace.meta(reply_intent="interested")
        trace.act("send")
        assert run(trace, CONDITIONAL) == []

    def test_condition_met_and_violated_fails(self, trace: TraceBuilder) -> None:
        trace.meta(reply_intent="unsubscribe")
        trace.act("send")
        assert run(trace, CONDITIONAL) == ["unsub"]

    def test_condition_met_and_respected_passes(self, trace: TraceBuilder) -> None:
        trace.meta(reply_intent="unsubscribe")
        trace.act("archive")
        assert run(trace, CONDITIONAL) == []

    def test_require_actions_under_a_condition(self, trace: TraceBuilder) -> None:
        rule = (
            "- id: review\n  kind: conditional\n  when: metadata.confidence < 0.8\n"
            "  require_actions: [human_review]\n"
        )
        trace.meta(confidence=0.5)
        trace.act("send")
        assert run(trace, rule) == ["review"]

        approved = TraceBuilder()
        approved.meta(confidence=0.5)
        approved.act("human_review")
        assert run(approved, rule) == []


class TestFinalState:
    def test_required_state_present_passes(self, trace: TraceBuilder) -> None:
        trace.act("send")
        trace.set_state(approval_status="approved")
        rule = "- id: ends\n  kind: final_state\n  require: state.approval_status == 'approved'\n"
        assert run(trace, rule) == []

    def test_wrong_final_state_fails(self, trace: TraceBuilder) -> None:
        trace.act("send")
        trace.set_state(approval_status="pending")
        rule = "- id: ends\n  kind: final_state\n  require: state.approval_status == 'approved'\n"
        assert run(trace, rule) == ["ends"]


class TestMaxRetries:
    def test_within_the_retry_budget_passes(self, trace: TraceBuilder) -> None:
        trace.act("fetch", args={"u": "x"}, status=Status.ERROR)
        trace.act("fetch", args={"u": "x"})
        assert run(trace, "- id: retries\n  kind: max_retries\n  max_retries: 2\n") == []

    def test_too_many_retries_fails(self, trace: TraceBuilder) -> None:
        for _ in range(4):
            trace.act("fetch", args={"u": "x"}, status=Status.ERROR)
        trace.act("fetch", args={"u": "x"})
        assert run(trace, "- id: retries\n  kind: max_retries\n  max_retries: 2\n") == ["retries"]


class TestIncompleteTrajectories:
    """The asymmetry that keeps verdicts sound."""

    def test_required_rules_are_inconclusive_when_spans_were_dropped(
        self, trace: TraceBuilder
    ) -> None:
        trace.act("research")
        trace.drop(2)
        loaded = load_policy(policy_yaml(ORDER))
        result = evaluate_policy(loaded, trace.build())
        assert result.inconclusive_rules == ["order"]
        assert not result.failures
        assert not result.passed  # inconclusive is not a pass

    def test_forbidden_rules_still_fire_on_an_incomplete_trace(self, trace: TraceBuilder) -> None:
        """Observing a forbidden action is valid evidence whatever else is missing."""
        trace.act("shell.exec")
        trace.drop(2)
        loaded = load_policy(policy_yaml(FORBIDDEN))
        result = evaluate_policy(loaded, trace.build())
        assert [f.rule_id for f in result.failures] == ["forbid"]
        assert not result.inconclusive_rules


class TestFailureQuality:
    def test_failures_carry_span_and_line_references(self, trace: TraceBuilder) -> None:
        trace.act("send", span_id="abc123")
        trace.act("approved")
        loaded = load_policy(policy_yaml(APPROVAL))
        failure = evaluate_policy(loaded, trace.build()).failures[0]

        assert failure.offending_span_id == "abc123"
        assert failure.offending_event_index == 0
        assert failure.policy_line is not None
        assert failure.expected is not None
        assert failure.actual is not None

    def test_rendered_output_names_what_where_and_expected(self, trace: TraceBuilder) -> None:
        trace.act("send", span_id="abc123")
        trace.act("approved")
        loaded = load_policy(policy_yaml(APPROVAL))
        rendered = evaluate_policy(loaded, trace.build()).format(policy_path="policies/p.yaml")

        assert "abc123" in rendered
        assert "expected" in rendered
        assert "policies/p.yaml:" in rendered
        assert "policy violation" not in rendered.lower()  # the banned non-message

    def test_custom_message_overrides_the_generated_one(self, trace: TraceBuilder) -> None:
        trace.act("send")
        rule = APPROVAL.rstrip() + '\n  message: "Email sent without approval."\n'
        loaded = load_policy(policy_yaml(rule))
        assert evaluate_policy(loaded, trace.build()).failures[0].message == (
            "Email sent without approval."
        )
