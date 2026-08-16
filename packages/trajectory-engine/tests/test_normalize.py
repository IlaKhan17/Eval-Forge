"""Normalization fixtures — one per rule in docs/TRAJECTORY_POLICIES.md §4.

This is the highest-value test file in the engine. Every case here has a correct
answer that is not obvious, and getting one wrong produces a confidently wrong
verdict rather than an obvious crash.
"""

from __future__ import annotations

from datetime import timedelta

from builders import BASE, TraceBuilder

from proofstep_trajectory import load_policy, normalize
from proofstep_trajectory.normalize import args_hash
from proofstep_types import SpanEvent, SpanType, Status

POLICY = load_policy(
    "name: p\nrules:\n  - id: r\n    kind: forbidden_action\n    actions: [nope]\n"
)


def events_of(builder: TraceBuilder, source: str | None = None) -> list[str]:
    policy = load_policy(source) if source else POLICY
    return [e.action for e in normalize(builder.build(), policy.policy).events]


class TestOrdering:
    def test_events_sort_by_start_time_not_end_time(self, trace: TraceBuilder) -> None:
        """An agent that *begins* a side effect early has violated the policy.

        A long-running call started first must sort first even if it finishes last.
        """
        trace.act("slow", at=BASE, duration_ms=5000)
        trace.act("fast", at=BASE + timedelta(milliseconds=10), duration_ms=1)
        assert events_of(trace) == ["slow", "fast"]

    def test_identical_timestamps_break_on_sequence_index(self, trace: TraceBuilder) -> None:
        trace.act("first", at=BASE)
        trace.act("second", at=BASE)
        trace.act("third", at=BASE)
        assert events_of(trace) == ["first", "second", "third"]

    def test_ordering_is_total_and_reproducible(self, trace: TraceBuilder) -> None:
        for i in range(10):
            trace.act(f"a{i}", at=BASE)
        once = events_of(trace)
        assert once == events_of(trace)


class TestSelection:
    def test_llm_spans_are_excluded_by_default(self, trace: TraceBuilder) -> None:
        """Otherwise every policy drowns in model calls."""
        trace.act("search")
        trace.llm("completion")
        trace.act("send")
        assert events_of(trace) == ["search", "send"]

    def test_agent_and_guardrail_spans_are_included(self, trace: TraceBuilder) -> None:
        trace.agent("research")
        trace.act("scan", span_type=SpanType.GUARDRAIL)
        assert events_of(trace) == ["research", "scan"]

    def test_span_types_can_be_opted_in(self, trace: TraceBuilder) -> None:
        trace.llm("completion")
        source = (
            "name: p\ninclude:\n  span_types: [llm]\n"
            "rules:\n  - id: r\n    kind: forbidden_action\n    actions: [nope]\n"
        )
        assert events_of(trace, source) == ["completion"]

    def test_exclude_names_supports_globs(self, trace: TraceBuilder) -> None:
        trace.act("log_debug")
        trace.act("metrics.emit")
        trace.act("send")
        source = (
            'name: p\ninclude:\n  exclude_names: ["log_*", "metrics.*"]\n'
            "rules:\n  - id: r\n    kind: forbidden_action\n    actions: [nope]\n"
        )
        assert events_of(trace, source) == ["send"]


class TestActionNaming:
    def test_explicit_attribute_wins_over_tool_name(self, trace: TraceBuilder) -> None:
        trace.act("raw_name")
        trace.spans[-1] = trace.spans[-1].model_copy(
            update={"attributes": {"proofstep.action": "canonical"}}
        )
        assert events_of(trace) == ["canonical"]

    def test_aliases_map_many_raw_names_to_one(self, trace: TraceBuilder) -> None:
        trace.act("gmail_send")
        trace.act("GmailSendTool")
        source = (
            "name: p\naliases:\n  gmail.send: [gmail_send, GmailSendTool]\n"
            "rules:\n  - id: r\n    kind: forbidden_action\n    actions: [nope]\n"
        )
        assert events_of(trace, source) == ["gmail.send", "gmail.send"]


class TestNesting:
    def test_parent_and_children_both_become_events(self, trace: TraceBuilder) -> None:
        parent = trace.agent("research")
        trace.act("web_search", parent=parent)
        trace.act("fetch", parent=parent)
        assert events_of(trace) == ["research", "web_search", "fetch"]

    def test_depth_is_retained(self, trace: TraceBuilder) -> None:
        parent = trace.agent("research")
        child = trace.act("web_search", parent=parent)
        trace.act("fetch", parent=child)
        events = normalize(trace.build(), POLICY.policy).events
        assert [e.depth for e in events] == [0, 1, 2]


class TestRetries:
    def test_repeat_after_failure_is_a_retry(self, trace: TraceBuilder) -> None:
        trace.act("fetch", args={"url": "x"}, status=Status.ERROR)
        trace.act("fetch", args={"url": "x"})
        events = normalize(trace.build(), POLICY.policy).events
        assert [e.is_retry for e in events] == [False, True]
        assert [e.attempt for e in events] == [1, 2]

    def test_repeat_after_success_is_not_a_retry(self, trace: TraceBuilder) -> None:
        """Two successful identical calls are two calls, not a retry."""
        trace.act("fetch", args={"url": "x"})
        trace.act("fetch", args={"url": "x"})
        events = normalize(trace.build(), POLICY.policy).events
        assert [e.is_retry for e in events] == [False, False]

    def test_different_arguments_are_not_a_retry(self, trace: TraceBuilder) -> None:
        trace.act("fetch", args={"url": "a"}, status=Status.ERROR)
        trace.act("fetch", args={"url": "b"})
        events = normalize(trace.build(), POLICY.policy).events
        assert [e.is_retry for e in events] == [False, False]

    def test_attempt_number_increments_across_repeated_failures(self, trace: TraceBuilder) -> None:
        for _ in range(3):
            trace.act("fetch", args={"url": "x"}, status=Status.ERROR)
        trace.act("fetch", args={"url": "x"})
        events = normalize(trace.build(), POLICY.policy).events
        assert [e.attempt for e in events] == [1, 2, 3, 4]


class TestParallel:
    def test_overlapping_siblings_share_a_group(self, trace: TraceBuilder) -> None:
        parent = trace.agent("fanout")
        trace.parallel("a", "b", "c", parent=parent)
        events = normalize(trace.build(), POLICY.policy).events
        groups = {e.action: e.parallel_group for e in events}
        assert groups["a"] is not None
        assert groups["a"] == groups["b"] == groups["c"]
        assert groups["fanout"] != groups["a"]

    def test_sequential_actions_are_not_grouped(self, trace: TraceBuilder) -> None:
        trace.act("a")
        trace.act("b")
        events = normalize(trace.build(), POLICY.policy).events
        assert all(e.parallel_group is None for e in events)


class TestFailedSpans:
    def test_failed_spans_still_produce_events(self, trace: TraceBuilder) -> None:
        """An attempted forbidden action is a violation even if the call failed.

        A gmail.send that returns 500 still tried to send.
        """
        trace.act("gmail.send", status=Status.ERROR)
        assert events_of(trace) == ["gmail.send"]

    def test_open_spans_are_treated_as_timed_out(self, trace: TraceBuilder) -> None:
        trace.act("hanging", open_ended=True)
        events = normalize(trace.build(), POLICY.policy).events
        assert events[0].status is Status.TIMEOUT


class TestIncompleteness:
    def test_dropped_spans_mark_the_trajectory_incomplete(self, trace: TraceBuilder) -> None:
        trace.act("a")
        trace.drop(3)
        norm = normalize(trace.build(), POLICY.policy)
        assert norm.incomplete
        assert any("dropped" in w for w in norm.warnings)

    def test_open_spans_mark_the_trajectory_incomplete(self, trace: TraceBuilder) -> None:
        trace.act("a", open_ended=True)
        assert normalize(trace.build(), POLICY.policy).incomplete

    def test_a_clean_trace_is_complete(self, trace: TraceBuilder) -> None:
        trace.act("a")
        assert not normalize(trace.build(), POLICY.policy).incomplete


class TestAnomalies:
    def test_duplicate_span_ids_are_deduplicated_with_a_warning(self, trace: TraceBuilder) -> None:
        trace.act("a", span_id="dup")
        trace.act("b", span_id="dup")
        norm = normalize(trace.build(), POLICY.policy)
        assert [e.action for e in norm.events] == ["a"]
        assert any("duplicate span id" in w for w in norm.warnings)

    def test_clock_skew_is_clamped_not_reordered(self, trace: TraceBuilder) -> None:
        parent = trace.agent("parent", at=BASE + timedelta(seconds=1), duration_ms=2000)
        trace.act("child", parent=parent, at=BASE)  # starts before its parent
        norm = normalize(trace.build(), POLICY.policy)
        assert [e.action for e in norm.events] == ["parent", "child"]
        assert any("clock skew" in w for w in norm.warnings)

    def test_empty_trace_yields_no_events(self, trace: TraceBuilder) -> None:
        assert events_of(trace) == []


class TestState:
    def test_trace_state_wins_over_metadata(self, trace: TraceBuilder) -> None:
        trace.meta(status="pending").set_state(status="approved")
        trace.act("a")
        assert normalize(trace.build(), POLICY.policy).state["status"] == "approved"

    def test_span_attributes_contribute_state(self, trace: TraceBuilder) -> None:
        trace.act("a")
        trace.spans[-1] = trace.spans[-1].model_copy(
            update={"attributes": {"proofstep.state.approval": "granted"}}
        )
        assert normalize(trace.build(), POLICY.policy).state["approval"] == "granted"

    def test_state_update_events_apply_in_order(self, trace: TraceBuilder) -> None:
        trace.act(
            "a",
            events=[
                SpanEvent(name="state_update", timestamp=BASE, attributes={"step": 1}),
                SpanEvent(
                    name="state_update",
                    timestamp=BASE + timedelta(seconds=1),
                    attributes={"step": 2},
                ),
            ],
        )
        assert normalize(trace.build(), POLICY.policy).state["step"] == 2


class TestArgsHash:
    def test_key_order_does_not_change_the_hash(self) -> None:
        assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})

    def test_different_values_change_the_hash(self) -> None:
        assert args_hash({"to": "a@x.com"}) != args_hash({"to": "b@x.com"})

    def test_empty_args_are_stable(self) -> None:
        assert args_hash({}) == args_hash({})
