"""The normalized trajectory event, and the result of evaluating a policy.

Rules never touch spans. They operate on this flat, ordered event list, so a
normalization fix corrects every rule at once instead of twelve times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from proofstep_types import Severity, Status


@dataclass(frozen=True, slots=True)
class TrajectoryEvent:
    index: int
    action: str
    span_id: str
    parent_span_id: str | None
    depth: int
    started_at: datetime
    ended_at: datetime | None
    status: Status
    args: dict[str, Any] = field(default_factory=dict)
    args_hash: str = ""
    attempt: int = 1
    is_retry: bool = False
    parallel_group: str | None = None
    result_summary: Any = None

    @property
    def failed(self) -> bool:
        return self.status in (Status.ERROR, Status.TIMEOUT)

    def key(self, parts: list[str]) -> tuple[Any, ...]:
        """Composite identity used by unique_action and no_loop."""
        values: list[Any] = []
        for part in parts:
            if part == "action":
                values.append(self.action)
            elif part == "args_hash":
                values.append(self.args_hash)
            elif part.startswith("args."):
                values.append(_dig(self.args, part[5:]))
            else:
                values.append(getattr(self, part, None))
        return tuple(values)


def _dig(source: dict[str, Any], path: str) -> Any:
    current: Any = source
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


@dataclass(frozen=True, slots=True)
class EventRef:
    """A pointer back to a span, so every finding is clickable."""

    index: int
    span_id: str
    action: str
    at: datetime


@dataclass(frozen=True, slots=True)
class PolicyFailure:
    rule_id: str
    rule_kind: str
    severity: Severity
    message: str
    offending_span_id: str | None = None
    offending_event_index: int | None = None
    offending_action: str = ""
    expected: Any = None
    actual: Any = None
    evidence: list[EventRef] = field(default_factory=list)
    policy_line: int | None = None

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCK

    def format(self, *, policy_path: str | None = None) -> str:
        """Human-readable rendering.

        Message quality is a feature. Every failure states what happened, where,
        what was expected, and how to go look — "policy violation" is banned.
        """
        mark = "✗" if self.blocking else "⚠"
        lines = [f"{mark} {self.rule_id}  [{self.severity.value}]", f"  {self.message}"]
        if self.offending_span_id:
            label = f"{self.offending_action} " if self.offending_action else ""
            where = f"    offending  : {label}span {self.offending_span_id}"
            if self.offending_event_index is not None:
                where += f" (event #{self.offending_event_index})"
            lines.append(where)
        if self.expected is not None:
            lines.append(f"    expected   : {self.expected}")
        if self.actual is not None:
            lines.append(f"    observed   : {self.actual}")
        if policy_path:
            suffix = f":{self.policy_line}" if self.policy_line else ""
            lines.append(f"    policy     : {policy_path}{suffix}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PolicyResult:
    policy_name: str
    failures: list[PolicyFailure] = field(default_factory=list)
    events: list[TrajectoryEvent] = field(default_factory=list)
    incomplete: bool = False
    inconclusive_rules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Inconclusive is not passed.

        A trajectory that lost spans cannot support an assertion about what did not
        happen, and reporting that as compliance would be a false negative in the
        direction that matters.
        """
        return not any(f.blocking for f in self.failures) and not self.inconclusive_rules

    @property
    def score(self) -> float:
        return 1.0 if self.passed else 0.0

    @property
    def blocking_failures(self) -> list[PolicyFailure]:
        return [f for f in self.failures if f.blocking]

    def format(self, *, policy_path: str | None = None) -> str:
        if not self.failures and not self.inconclusive_rules:
            return f"✓ {self.policy_name}: {len(self.events)} events, no violations"
        blocks = [f.format(policy_path=policy_path) for f in self.failures]
        for rule_id in self.inconclusive_rules:
            blocks.append(
                f"? {rule_id}  [inconclusive]\n"
                "  The trajectory is incomplete (spans were dropped or left open), so "
                "this rule cannot assert that something did not happen."
            )
        return "\n\n".join(blocks)
