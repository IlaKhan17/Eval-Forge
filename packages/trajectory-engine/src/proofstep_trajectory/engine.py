"""Top-level evaluation: (policy, trace) -> PolicyResult."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from proofstep_trajectory.events import PolicyFailure, PolicyResult
from proofstep_trajectory.matchers import MATCHERS, REQUIRES_COMPLETE
from proofstep_trajectory.normalize import Normalized, normalize
from proofstep_trajectory.parser import LoadedPolicy, check_actions
from proofstep_trajectory.predicates import PredicateError, evaluate
from proofstep_types import Trace


def evaluate_policy(loaded: LoadedPolicy, trace: Trace) -> PolicyResult:
    """Evaluate every rule in the policy against one trace."""
    policy = loaded.policy
    norm = normalize(trace, policy)

    failures: list[PolicyFailure] = []
    inconclusive: list[str] = []
    warnings = [*norm.warnings, *check_actions(loaded, norm.actions)]

    for rule in policy.rules:
        # Absence of evidence is not evidence of absence: a rule that asserts
        # something *did* happen cannot be judged on a trajectory that lost spans.
        # Rules that observe a forbidden action still run, because seeing it is
        # valid evidence no matter what is missing.
        if norm.incomplete and rule.kind in REQUIRES_COMPLETE:
            inconclusive.append(rule.id)
            continue

        if rule.when is not None and rule.kind != "conditional":
            if not _guard(rule, norm, warnings):
                continue
        elif rule.kind == "conditional" and not _guard(rule, norm, warnings):
            continue

        for failure in MATCHERS[rule.kind](rule, norm):
            failures.append(
                failure if failure.policy_line else _with_line(failure, loaded.line_for(rule.id))
            )

    return PolicyResult(
        policy_name=policy.name,
        failures=failures,
        events=norm.events,
        incomplete=norm.incomplete,
        inconclusive_rules=inconclusive,
        warnings=warnings,
    )


def _guard(rule: Any, norm: Normalized, warnings: list[str]) -> bool:
    """Evaluate a rule's `when` predicate. A broken predicate must not pass silently."""
    namespace = {
        "metadata": norm.state,
        "state": norm.state,
        "actions": dict.fromkeys(norm.actions, True),
        "counts": {action: norm.counts(action) for action in norm.actions},
    }
    try:
        return evaluate(rule.when, namespace)
    except PredicateError as exc:
        warnings.append(f"rule {rule.id!r}: `when` could not be evaluated ({exc}); rule skipped")
        return False


def _with_line(failure: PolicyFailure, line: int | None) -> PolicyFailure:
    return replace(failure, policy_line=line)
