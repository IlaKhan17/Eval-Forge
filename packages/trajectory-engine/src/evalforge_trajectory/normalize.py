"""Lower a span tree into an ordered event list.

This module is the highest-risk code in the engine. Every rule below has a correct
answer that is not obvious, and getting one wrong produces a *confidently wrong
verdict*, which is worse than no verdict. Each is specified in
docs/TRAJECTORY_POLICIES.md §4 and fixture-tested individually.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from evalforge_trajectory.events import TrajectoryEvent
from evalforge_trajectory.schema import Include, Policy
from evalforge_types import Span, Status, Trace

# Spans overlapping by less than this are treated as concurrent rather than ordered.
PARALLEL_TOLERANCE = timedelta(milliseconds=0)


class Normalized:
    """Events plus the derived state and diagnostics the matchers need."""

    __slots__ = ("_by_action", "events", "incomplete", "state", "warnings")

    def __init__(
        self,
        events: list[TrajectoryEvent],
        state: dict[str, Any],
        *,
        incomplete: bool,
        warnings: list[str],
    ) -> None:
        self.events = events
        self.state = state
        self.incomplete = incomplete
        self.warnings = warnings
        self._by_action: dict[str, list[TrajectoryEvent]] = {}
        for event in events:
            self._by_action.setdefault(event.action, []).append(event)

    def of(self, action: str) -> list[TrajectoryEvent]:
        return self._by_action.get(action, [])

    def first(self, action: str) -> TrajectoryEvent | None:
        found = self.of(action)
        return found[0] if found else None

    def last(self, action: str) -> TrajectoryEvent | None:
        found = self.of(action)
        return found[-1] if found else None

    @property
    def actions(self) -> set[str]:
        return set(self._by_action)

    def counts(self, action: str, *, include_retries: bool = False) -> int:
        events = self.of(action)
        return len(events) if include_retries else sum(1 for e in events if not e.is_retry)


def normalize(trace: Trace, policy: Policy) -> Normalized:
    """Produce the event list a policy is evaluated against."""
    warnings: list[str] = []
    alias_map = _alias_map(policy)

    spans = _select(trace.spans, policy.include, alias_map, warnings)
    spans = _dedupe(spans, warnings)
    spans = _fix_clock_skew(spans, trace, warnings)
    ordered = sorted(spans, key=_sort_key)

    groups = _parallel_groups(ordered)
    events = _build(ordered, alias_map, groups)
    events = _mark_retries(events)

    incomplete = trace.dropped_span_count > 0 or any(s.is_open for s in trace.spans)
    if trace.dropped_span_count:
        warnings.append(
            f"{trace.dropped_span_count} span(s) were dropped by the exporter; "
            "assertions about what did not happen are unsound on this trace."
        )
    if any(s.is_open for s in trace.spans):
        warnings.append("The trace contains spans that never ended.")

    return Normalized(events, _state(trace), incomplete=incomplete, warnings=warnings)


# --------------------------------------------------------------------- selection


def _alias_map(policy: Policy) -> dict[str, str]:
    """raw name -> canonical name."""
    return {raw: canonical for canonical, raws in policy.aliases.items() for raw in raws}


def action_of(span: Span, alias_map: dict[str, str]) -> str:
    """Precedence: explicit attribute, then tool name, then span name."""
    raw = span.attributes.get("evalforge.action") or span.tool_name or span.name
    return alias_map.get(str(raw), str(raw))


def _select(
    spans: Sequence[Span],
    include: Include,
    alias_map: dict[str, str],
    warnings: list[str],
) -> list[Span]:
    wanted = set(include.span_types)
    selected: list[Span] = []
    for span in spans:
        if span.span_type not in wanted:
            continue
        action = action_of(span, alias_map)
        if any(fnmatch.fnmatch(action, pattern) for pattern in include.exclude_names):
            continue
        selected.append(span)

    if not selected and spans:
        warnings.append(
            f"No spans matched include.span_types={sorted(t.value for t in wanted)}. "
            f"The trace contains: {sorted({s.span_type.value for s in spans})}."
        )
    return selected


def _dedupe(spans: Sequence[Span], warnings: list[str]) -> list[Span]:
    """Buggy instrumentation can emit a span id twice. Keep the first, warn once."""
    seen: set[str] = set()
    unique: list[Span] = []
    duplicates = 0
    for span in spans:
        if span.span_id in seen:
            duplicates += 1
            continue
        seen.add(span.span_id)
        unique.append(span)
    if duplicates:
        warnings.append(f"{duplicates} duplicate span id(s) were ignored.")
    return unique


def _fix_clock_skew(spans: Sequence[Span], trace: Trace, warnings: list[str]) -> list[Span]:
    """Clamp a child that claims to start before its parent.

    Never reorder silently: a child appearing before its parent would change every
    order verdict, so the clamp is explicit and warned about.
    """
    by_id = {s.span_id: s for s in trace.spans}
    fixed: list[Span] = []
    skewed = 0
    for span in spans:
        parent = by_id.get(span.parent_span_id) if span.parent_span_id else None
        if parent is not None and span.started_at < parent.started_at:
            skewed += 1
            fixed.append(span.model_copy(update={"started_at": parent.started_at}))
        else:
            fixed.append(span)
    if skewed:
        warnings.append(f"{skewed} span(s) started before their parent; clamped (clock skew).")
    return fixed


def _sort_key(span: Span) -> tuple[Any, ...]:
    """Order by *start* time.

    Start, not end: an agent that begins sending before approval has violated the
    policy regardless of when the call returned. Ties break on the SDK's monotonic
    counter, then span id, so ordering is total and reproducible.
    """
    return (span.started_at, span.sequence_index, span.span_id)


# ------------------------------------------------------------------ construction


def _depth_of(span: Span, by_id: dict[str, Span], cache: dict[str, int]) -> int:
    if span.span_id in cache:
        return cache[span.span_id]
    depth = 0
    current = span
    seen: set[str] = {span.span_id}
    while current.parent_span_id and current.parent_span_id in by_id:
        if current.parent_span_id in seen:  # defensive: a cycle in parent links
            break
        seen.add(current.parent_span_id)
        current = by_id[current.parent_span_id]
        depth += 1
    cache[span.span_id] = depth
    return depth


def _parallel_groups(ordered: Sequence[Span]) -> dict[str, str]:
    """Group siblings whose execution overlaps in time.

    Within a group no order is asserted. `forbidden_before` then uses conservative
    semantics — a race is not proof of a violation, and false-positive policy
    failures destroy trust in the gate faster than false negatives do.
    """
    groups: dict[str, str] = {}
    for i, span in enumerate(ordered):
        if span.ended_at is None:
            continue
        for other in ordered[i + 1 :]:
            if other.parent_span_id != span.parent_span_id:
                continue
            if other.started_at >= span.ended_at + PARALLEL_TOLERANCE:
                break
            group = groups.get(span.span_id) or f"pg-{span.span_id[:8]}"
            groups[span.span_id] = group
            groups[other.span_id] = group
    return groups


def _build(
    ordered: Sequence[Span], alias_map: dict[str, str], groups: dict[str, str]
) -> list[TrajectoryEvent]:
    by_id = {s.span_id: s for s in ordered}
    cache: dict[str, int] = {}
    events: list[TrajectoryEvent] = []
    for index, span in enumerate(ordered):
        args = span.tool_args or {}
        events.append(
            TrajectoryEvent(
                index=index,
                action=action_of(span, alias_map),
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                depth=_depth_of(span, by_id, cache),
                started_at=span.started_at,
                ended_at=span.ended_at,
                status=span.status if span.ended_at else Status.TIMEOUT,
                args=args,
                args_hash=args_hash(args),
                parallel_group=groups.get(span.span_id),
                result_summary=span.output,
            )
        )
    return events


def args_hash(args: dict[str, Any]) -> str:
    """Stable hash of canonicalized arguments.

    Computed over the values as captured, so duplicate detection still works on a
    field the SDK redacted — you can catch two sends to the same recipient without
    ever storing the address.
    """
    canonical = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _mark_retries(events: Sequence[TrajectoryEvent]) -> list[TrajectoryEvent]:
    """Detect retries: same action and args, immediately following a failure.

    The counting semantics that follow from this matter and are easy to get wrong:
      - retries do NOT count toward `limit.max_calls` — a flaky network should not
        read as a policy violation;
      - retries DO count toward `max_retries`, obviously;
      - a retried side effect DOES count toward `unique_action`, because two actual
        sends sent two actual emails whatever the agent intended.
    """
    marked: list[TrajectoryEvent] = []
    last_failure: dict[tuple[str, str], int] = {}

    for event in events:
        signature = (event.action, event.args_hash)
        previous_attempt = last_failure.get(signature)
        if previous_attempt is not None:
            marked.append(
                TrajectoryEvent(
                    **{
                        **_as_dict(event),
                        "attempt": previous_attempt + 1,
                        "is_retry": True,
                    }
                )
            )
        else:
            marked.append(event)

        current = marked[-1]
        if current.failed:
            last_failure[signature] = current.attempt
        else:
            last_failure.pop(signature, None)

    return marked


def _as_dict(event: TrajectoryEvent) -> dict[str, Any]:
    return {slot: getattr(event, slot) for slot in TrajectoryEvent.__slots__}


def _state(trace: Trace) -> dict[str, Any]:
    """Merge state sources in increasing precedence.

    trace metadata -> `evalforge.state.*` span attributes -> explicit state_update
    span events. Later writes win, so the final state reflects the last thing the
    workflow actually recorded.
    """
    state: dict[str, Any] = dict(trace.metadata)

    ordered = sorted(trace.spans, key=lambda s: (s.started_at, s.sequence_index, s.span_id))
    for span in ordered:
        for key, value in span.attributes.items():
            if key.startswith("evalforge.state."):
                state[key.removeprefix("evalforge.state.")] = value
        for event in sorted(span.events, key=lambda e: e.timestamp):
            if event.name == "state_update":
                state.update(event.attributes)

    state.update(trace.state)
    return state
