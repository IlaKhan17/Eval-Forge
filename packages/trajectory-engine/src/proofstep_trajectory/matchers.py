"""The twelve rule matchers.

Each takes a rule and the normalized trajectory and returns failures. All operate on
events, never on spans, so a normalization fix corrects every rule at once.

Complexity is O(events) per rule with the prebuilt indexes, so a whole policy over a
200-event trace evaluates in microseconds. That is what makes it affordable to run
policies on 100% of production traces rather than a sample.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from proofstep_trajectory import schema as rules_schema
from proofstep_trajectory.events import EventRef, PolicyFailure, TrajectoryEvent
from proofstep_trajectory.normalize import Normalized
from proofstep_trajectory.predicates import PredicateError, evaluate

Matcher = Callable[[Any, Normalized], list[PolicyFailure]]

# Rules that assert something *did* happen cannot be evaluated on an incomplete
# trajectory: absence of evidence is not evidence of absence when spans were lost.
# Rules that observe a forbidden action still can — seeing it is valid evidence
# regardless of what is missing. This asymmetry is the single most important
# correctness property in the engine.
REQUIRES_COMPLETE = frozenset({"required_order", "required_action", "limit", "conditional"})


def _ref(event: TrajectoryEvent) -> EventRef:
    return EventRef(
        index=event.index, span_id=event.span_id, action=event.action, at=event.started_at
    )


def _fail(
    rule: Any,
    message: str,
    *,
    offending: TrajectoryEvent | None = None,
    expected: Any = None,
    actual: Any = None,
    evidence: Sequence[TrajectoryEvent] = (),
) -> PolicyFailure:
    refs = [_ref(e) for e in evidence] or ([_ref(offending)] if offending else [])
    return PolicyFailure(
        rule_id=rule.id,
        rule_kind=rule.kind,
        severity=rule.severity,
        message=rule.message or message,
        offending_span_id=offending.span_id if offending else None,
        offending_event_index=offending.index if offending else None,
        offending_action=offending.action if offending else "",
        expected=expected,
        actual=actual,
        evidence=refs,
    )


def _visible(rule: Any, events: Sequence[TrajectoryEvent]) -> list[TrajectoryEvent]:
    return [e for e in events if not (rule.ignore_failed and e.failed)]


# --------------------------------------------------------------------- ordering


def required_order(rule: rules_schema.RequiredOrder, norm: Normalized) -> list[PolicyFailure]:
    """Greedy subsequence match, reporting where matching stopped.

    "Order violated" is useless. Naming the step that could not be matched, and the
    position matching reached, is what makes the failure actionable.
    """
    events = _visible(rule, norm.events)
    if rule.mode == "contiguous":
        return _contiguous_order(rule, events)

    position = 0
    matched: list[TrajectoryEvent] = []
    for step in rule.steps:
        found = next((e for e in events[position:] if e.action == step), None)
        if found is None:
            seen = [e.action for e in events]
            return [
                _fail(
                    rule,
                    f"Required step {step!r} did not occur after {matched[-1].action!r}"
                    if matched
                    else f"Required step {step!r} never occurred.",
                    offending=matched[-1] if matched else None,
                    expected=" -> ".join(rule.steps),
                    actual=(
                        f"matched {len(matched)}/{len(rule.steps)} steps; "
                        f"observed order: {' -> '.join(seen) or '<no events>'}"
                    ),
                    evidence=matched,
                )
            ]
        matched.append(found)
        position = events.index(found) + 1
    return []


def _contiguous_order(
    rule: rules_schema.RequiredOrder, events: Sequence[TrajectoryEvent]
) -> list[PolicyFailure]:
    actions = [e.action for e in events]
    width = len(rule.steps)
    for start in range(len(actions) - width + 1):
        if actions[start : start + width] == rule.steps:
            return []
    return [
        _fail(
            rule,
            f"Steps {' -> '.join(rule.steps)} did not occur contiguously.",
            expected=" -> ".join(rule.steps),
            actual=" -> ".join(actions) or "<no events>",
        )
    ]


# --------------------------------------------------------------- presence rules


def required_action(rule: rules_schema.RequiredAction, norm: Normalized) -> list[PolicyFailure]:
    count = len(_visible(rule, norm.of(rule.action)))
    if count >= rule.min_count:
        return []
    return [
        _fail(
            rule,
            f"{rule.action!r} occurred {count} time(s); at least {rule.min_count} required.",
            expected=f"{rule.action} >= {rule.min_count}",
            actual=count,
        )
    ]


def forbidden_action(rule: rules_schema.ForbiddenAction, norm: Normalized) -> list[PolicyFailure]:
    failures: list[PolicyFailure] = []
    for action in rule.actions:
        occurrences = _visible(rule, norm.of(action))
        if occurrences:
            failures.append(
                _fail(
                    rule,
                    f"Forbidden action {action!r} was invoked {len(occurrences)} time(s).",
                    offending=occurrences[0],
                    expected=f"{action} must never occur",
                    actual=f"{len(occurrences)} occurrence(s)",
                    evidence=occurrences[:5],
                )
            )
    return failures


def forbidden_before(rule: rules_schema.ForbiddenBefore, norm: Normalized) -> list[PolicyFailure]:
    """`action` must not occur before `before` has occurred.

    Vacuously true when `before` never happens at all — the most common author
    surprise, documented explicitly. Use `required_action` to demand it happened.
    """
    gate = norm.first(rule.before)
    offenders = _visible(rule, norm.of(rule.action))
    if not offenders:
        return []

    if gate is None:
        first = offenders[0]
        return [
            _fail(
                rule,
                f"{rule.action!r} occurred but {rule.before!r} never did.",
                offending=first,
                expected=f"{rule.before} must occur before {rule.action}",
                actual=f"{rule.before} never occurred",
                evidence=offenders[:5],
            )
        ]

    early = [e for e in offenders if _strictly_before(e, gate)]
    if not early:
        return []

    first = early[0]
    delay = (gate.started_at - first.started_at).total_seconds()
    return [
        _fail(
            rule,
            f"{rule.action!r} occurred before {rule.before!r}.",
            offending=first,
            expected=f"{rule.before} must occur before {rule.action}",
            actual=(
                f"{rule.before} occurred at {gate.started_at.time()} "
                f"(event #{gate.index}), {delay:.2f}s later"
            ),
            evidence=[first, gate],
        )
    ]


def _strictly_before(earlier: TrajectoryEvent, later: TrajectoryEvent) -> bool:
    """Conservative ordering: concurrent events are not ordered.

    If the two overlap, we do not claim one preceded the other. A race is not proof
    of a violation, and a false-positive policy failure destroys trust in the gate
    faster than a false negative does.
    """
    if earlier.parallel_group and earlier.parallel_group == later.parallel_group:
        return False
    return earlier.started_at < later.started_at


def forbidden_after(rule: rules_schema.ForbiddenAfter, norm: Normalized) -> list[PolicyFailure]:
    gate = norm.first(rule.after)
    if gate is None:
        return []
    late = [e for e in _visible(rule, norm.of(rule.action)) if _strictly_before(gate, e)]
    if not late:
        return []
    return [
        _fail(
            rule,
            f"{rule.action!r} occurred after {rule.after!r}.",
            offending=late[0],
            expected=f"{rule.action} must not occur after {rule.after}",
            actual=f"{len(late)} occurrence(s) after event #{gate.index}",
            evidence=[gate, *late[:4]],
        )
    ]


# ----------------------------------------------------------------------- budgets


def limit(rule: rules_schema.Limit, norm: Normalized) -> list[PolicyFailure]:
    """Retries are excluded from call budgets: a flaky network is not overuse."""
    events = [e for e in _visible(rule, norm.of(rule.action)) if not e.is_retry]
    count = len(events)
    failures: list[PolicyFailure] = []

    if rule.max_calls is not None and count > rule.max_calls:
        excess = events[rule.max_calls :]
        failures.append(
            _fail(
                rule,
                f"{rule.action!r} was called {count} times; the limit is {rule.max_calls}.",
                offending=excess[0],
                expected=f"at most {rule.max_calls} call(s)",
                actual=f"{count} call(s); excess at events "
                f"{', '.join(f'#{e.index}' for e in excess[:5])}",
                evidence=excess[:5],
            )
        )

    if rule.min_calls is not None and count < rule.min_calls:
        failures.append(
            _fail(
                rule,
                f"{rule.action!r} was called {count} times; at least {rule.min_calls} required.",
                expected=f"at least {rule.min_calls} call(s)",
                actual=count,
            )
        )
    return failures


def unique_action(rule: rules_schema.UniqueAction, norm: Normalized) -> list[PolicyFailure]:
    """Duplicate side effects.

    Retries DO count here. Two actual sends sent two actual emails, whatever the
    agent intended by them.
    """
    seen: dict[tuple[Any, ...], TrajectoryEvent] = {}
    failures: list[PolicyFailure] = []
    for event in _visible(rule, norm.of(rule.action)):
        key = event.key(rule.key)
        if (first := seen.get(key)) is not None:
            failures.append(
                _fail(
                    rule,
                    f"{rule.action!r} was performed twice with the same {'+'.join(rule.key)}.",
                    offending=event,
                    expected=f"at most one {rule.action} per {'+'.join(rule.key)}",
                    actual=f"first at event #{first.index}, repeated at event #{event.index}",
                    evidence=[first, event],
                )
            )
        else:
            seen[key] = event
    return failures


def no_loop(rule: rules_schema.NoLoop, norm: Normalized) -> list[PolicyFailure]:
    """Sliding-window repetition detection."""
    events = _visible(rule, norm.events)
    for start in range(len(events)):
        window = events[start : start + rule.window]
        if len(window) < rule.min_repeats:
            break
        counts: dict[tuple[Any, ...], list[TrajectoryEvent]] = {}
        for event in window:
            counts.setdefault(event.key(rule.key), []).append(event)
        for repeated in counts.values():
            if len(repeated) >= rule.min_repeats:
                return [
                    _fail(
                        rule,
                        f"{repeated[0].action!r} repeated {len(repeated)} times within "
                        f"{rule.window} events — the agent appears to be looping.",
                        offending=repeated[0],
                        expected=f"fewer than {rule.min_repeats} repeats per {rule.window} events",
                        actual=f"events {', '.join(f'#{e.index}' for e in repeated)}",
                        evidence=repeated[:5],
                    )
                ]
    return []


def max_retries(rule: rules_schema.MaxRetries, norm: Normalized) -> list[PolicyFailure]:
    events = norm.events if rule.action == "*" else norm.of(rule.action)
    worst: dict[str, TrajectoryEvent] = {}
    for event in events:
        if event.attempt > worst.get(event.action, event).attempt or event.action not in worst:
            worst[event.action] = event

    failures: list[PolicyFailure] = []
    for action, event in sorted(worst.items()):
        retries = event.attempt - 1
        if retries > rule.max_retries:
            failures.append(
                _fail(
                    rule,
                    f"{action!r} was retried {retries} times; the limit is {rule.max_retries}.",
                    offending=event,
                    expected=f"at most {rule.max_retries} retries",
                    actual=f"{retries} retries",
                )
            )
    return failures


# -------------------------------------------------------------------- predicates


def argument_condition(
    rule: rules_schema.ArgumentCondition, norm: Normalized
) -> list[PolicyFailure]:
    failures: list[PolicyFailure] = []
    for event in _visible(rule, norm.of(rule.action)):
        namespace = {
            "args": event.args,
            "metadata": norm.state,
            "state": norm.state,
            "action": event.action,
        }
        try:
            ok = evaluate(rule.require, namespace)
        except PredicateError as exc:
            failures.append(
                _fail(
                    rule,
                    f"Condition on {rule.action!r} could not be evaluated: {exc}",
                    offending=event,
                )
            )
            continue
        if not ok:
            failures.append(
                _fail(
                    rule,
                    f"{rule.action!r} was called with arguments that violate the policy.",
                    offending=event,
                    expected=rule.require,
                    actual=f"args={_summarize(event.args)}",
                    evidence=[event],
                )
            )
    return failures


def conditional(rule: rules_schema.Conditional, norm: Normalized) -> list[PolicyFailure]:
    """`when` has already been checked by the caller; apply the consequences."""
    failures: list[PolicyFailure] = []

    for action in rule.require_actions:
        if not _visible(rule, norm.of(action)):
            failures.append(
                _fail(
                    rule,
                    f"Condition ({rule.when}) held, so {action!r} was required but never occurred.",
                    expected=f"{action} required when: {rule.when}",
                    actual=f"{action} did not occur",
                )
            )

    for action in rule.forbid_actions:
        occurrences = _visible(rule, norm.of(action))
        if occurrences:
            failures.append(
                _fail(
                    rule,
                    f"Condition ({rule.when}) held, so {action!r} was forbidden but occurred "
                    f"{len(occurrences)} time(s).",
                    offending=occurrences[0],
                    expected=f"{action} forbidden when: {rule.when}",
                    actual=f"{len(occurrences)} occurrence(s)",
                    evidence=occurrences[:5],
                )
            )
    return failures


def final_state(rule: rules_schema.FinalState, norm: Normalized) -> list[PolicyFailure]:
    namespace = {"state": norm.state, "metadata": norm.state}
    try:
        ok = evaluate(rule.require, namespace)
    except PredicateError as exc:
        return [_fail(rule, f"Final-state condition could not be evaluated: {exc}")]
    if ok:
        return []
    return [
        _fail(
            rule,
            "The workflow did not end in the required state.",
            expected=rule.require,
            actual=f"state={_summarize(norm.state)}",
        )
    ]


def _summarize(data: dict[str, Any], limit_: int = 6) -> str:
    items = list(data.items())[:limit_]
    rendered = ", ".join(f"{k}={v!r}" for k, v in items)
    return f"{{{rendered}{', …' if len(data) > limit_ else ''}}}"


MATCHERS: dict[str, Matcher] = {
    "required_order": required_order,
    "required_action": required_action,
    "forbidden_action": forbidden_action,
    "forbidden_before": forbidden_before,
    "forbidden_after": forbidden_after,
    "limit": limit,
    "unique_action": unique_action,
    "no_loop": no_loop,
    "argument_condition": argument_condition,
    "conditional": conditional,
    "final_state": final_state,
    "max_retries": max_retries,
}

assert set(MATCHERS) == rules_schema.RULE_KINDS, "every rule kind needs a matcher"
