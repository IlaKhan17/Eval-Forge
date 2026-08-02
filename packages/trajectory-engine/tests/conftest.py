"""Trace builders for policy fixtures.

Building traces by hand in every test buries the interesting detail under
boilerplate. These helpers make the *shape* of each scenario the visible part.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evalforge_types import Span, SpanEvent, SpanType, Status, Trace

BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


class TraceBuilder:
    """Append spans on a virtual clock that advances by `step` unless told otherwise."""

    def __init__(self, name: str = "workflow", *, step_ms: int = 100) -> None:
        self.name = name
        self.step = timedelta(milliseconds=step_ms)
        self.clock = BASE
        self.spans: list[Span] = []
        self.metadata: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.dropped = 0
        self._counter = 0

    def act(
        self,
        action: str,
        *,
        span_type: SpanType = SpanType.TOOL,
        args: dict[str, Any] | None = None,
        status: Status = Status.OK,
        duration_ms: int = 50,
        parent: str | None = None,
        at: datetime | None = None,
        span_id: str | None = None,
        open_ended: bool = False,
        events: list[SpanEvent] | None = None,
        output: Any = None,
    ) -> str:
        started = at or self.clock
        self._counter += 1
        identifier = span_id or f"span{self._counter:02d}"
        ended = None if open_ended else started + timedelta(milliseconds=duration_ms)

        self.spans.append(
            Span(
                span_id=identifier,
                trace_id="trace-1",
                parent_span_id=parent,
                name=action,
                span_type=span_type,
                tool_name=action if span_type is SpanType.TOOL else None,
                tool_args=args,
                status=status,
                started_at=started,
                ended_at=ended,
                sequence_index=self._counter,
                events=events or [],
                output=output,
            )
        )
        if at is None:
            self.clock = started + self.step
        return identifier

    def agent(self, action: str, **kw: Any) -> str:
        return self.act(action, span_type=SpanType.AGENT, **kw)

    def llm(self, name: str = "completion", **kw: Any) -> str:
        return self.act(name, span_type=SpanType.LLM, **kw)

    def parallel(
        self, *actions: str, duration_ms: int = 200, parent: str | None = None
    ) -> list[str]:
        """Actions that genuinely overlap in time, sharing a parent."""
        start = self.clock
        ids = [
            self.act(a, at=start, duration_ms=duration_ms, parent=parent, args={"i": i})
            for i, a in enumerate(actions)
        ]
        self.clock = start + timedelta(milliseconds=duration_ms) + self.step
        return ids

    def set_state(self, **values: Any) -> TraceBuilder:
        self.state.update(values)
        return self

    def meta(self, **values: Any) -> TraceBuilder:
        self.metadata.update(values)
        return self

    def drop(self, count: int = 1) -> TraceBuilder:
        self.dropped += count
        return self

    def build(self) -> Trace:
        return Trace(
            trace_id="trace-1",
            name=self.name,
            started_at=BASE,
            ended_at=self.clock,
            spans=self.spans,
            metadata=self.metadata,
            state=self.state,
            dropped_span_count=self.dropped,
        )


@pytest.fixture
def trace() -> TraceBuilder:
    return TraceBuilder()


def policy_yaml(*rules: str, name: str = "test-policy", extra: str = "") -> str:
    body = "\n".join(f"  {line}" for rule in rules for line in rule.strip().splitlines())
    return f"name: {name}\n{extra}rules:\n{body}\n"
