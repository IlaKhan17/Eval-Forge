"""Context propagation across every way Python spawns concurrent work.

This is the matrix that decides whether spans attach to the right parent. Getting it
wrong produces a flat or orphaned trace, which then produces wrong trajectory
verdicts — the failure is silent and downstream.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import RecordingTransport  # type: ignore[attr-defined]

import evalforge
from evalforge import safety
from evalforge.client import Client
from evalforge.config import Config


def tree(trace) -> dict[str, str | None]:  # type: ignore[no-untyped-def]
    """name -> parent name, for readable assertions."""
    snapshot = trace.snapshot()
    by_id = {s.span_id: s.name for s in snapshot.spans}
    return {s.name: by_id.get(s.parent_span_id) for s in snapshot.spans}


class TestNesting:
    def test_nested_context_managers_build_a_tree(self, client: Client) -> None:
        with client.trace("root") as trace:
            with client.span("outer"), client.span("inner"):
                pass
            with client.span("sibling"):
                pass

        assert tree(trace) == {"outer": None, "inner": "outer", "sibling": None}

    def test_depth_increments(self, client: Client) -> None:
        with client.trace("root"), client.span("a") as a, client.span("b") as b:
            assert a.depth == 0
            assert b.depth == 1

    def test_context_is_restored_after_a_span_ends(self, client: Client) -> None:
        with client.trace("root"):
            assert evalforge.current_span() is None
            with client.span("a") as a:
                assert evalforge.current_span() is a
            assert evalforge.current_span() is None

    def test_context_is_restored_after_an_exception(self, client: Client) -> None:
        with client.trace("root"):
            try:
                with client.span("boom"):
                    msg = "x"
                    raise ValueError(msg)
            except ValueError:
                pass
            assert evalforge.current_span() is None


class TestAsyncPropagation:
    async def test_gather_children_attach_to_the_right_parent(self, client: Client) -> None:
        async def child(name: str) -> None:
            with client.span(name):
                await asyncio.sleep(0.001)

        with client.trace("root") as trace, client.span("parent"):
            await asyncio.gather(child("a"), child("b"), child("c"))

        result = tree(trace)
        assert result["a"] == result["b"] == result["c"] == "parent"

    async def test_task_group_children_attach_correctly(self, client: Client) -> None:
        async def child(name: str) -> None:
            with client.span(name):
                await asyncio.sleep(0.001)

        with client.trace("root") as trace, client.span("parent"):
            async with asyncio.TaskGroup() as group:
                for name in ("x", "y"):
                    group.create_task(child(name))

        result = tree(trace)
        assert result["x"] == result["y"] == "parent"

    async def test_create_task_inherits_the_context(self, client: Client) -> None:
        async def child() -> None:
            with client.span("detached"):
                await asyncio.sleep(0)

        with client.trace("root") as trace, client.span("parent"):
            task = asyncio.create_task(child())
            await task

        assert tree(trace)["detached"] == "parent"

    async def test_sibling_tasks_do_not_see_each_others_spans(self, client: Client) -> None:
        """Each task gets a copy of the context, so nesting cannot leak sideways."""
        seen: list[str | None] = []

        async def child(name: str) -> None:
            with client.span(name):
                await asyncio.sleep(0.005)
                current = evalforge.current_span()
                seen.append(current.name if current else None)

        with client.trace("root"), client.span("parent"):
            await asyncio.gather(child("a"), child("b"))

        assert sorted(n for n in seen if n) == ["a", "b"]

    async def test_concurrent_spans_overlap_in_time(self, client: Client) -> None:
        """Which is what makes the trajectory engine treat them as unordered."""

        async def child(name: str) -> None:
            with client.span(name):
                await asyncio.sleep(0.02)

        with client.trace("root") as trace, client.span("parent"):
            await asyncio.gather(child("a"), child("b"))

        spans = {s.name: s for s in trace.snapshot().spans}
        assert spans["a"].started_at < spans["b"].ended_at
        assert spans["b"].started_at < spans["a"].ended_at


class TestThreadPropagation:
    def test_a_thread_without_propagate_still_records_the_span(self) -> None:
        """Python does not copy contextvars across threads, so the worker sees no
        active trace. The span must still survive — as its own trace, with a warning
        pointing at `propagate()`. Losing it silently would break the SDK's promise
        that data loss is always visible.
        """
        transport = RecordingTransport()
        client = Client(
            Config(project="p", api_key="k", export=True, flush_interval_s=0.01),
            transport=transport,
        )

        with client.trace("root"), client.span("parent"), ThreadPoolExecutor(1) as pool:
            pool.submit(lambda: _work(client, "worker")).result()

        client.flush(2.0)
        names = {span["name"] for payload in transport.payloads() for span in payload["spans"]}
        assert "worker" in names, "the threaded span was lost entirely"
        client.shutdown(0.1)

    def test_the_orphan_warning_names_the_fix(self, caplog: pytest.LogCaptureFixture) -> None:
        safety.reset_log_throttle()
        client = Client(Config(project="p", export=False))
        with caplog.at_level("WARNING", logger="evalforge"), client.span("lonely"):
            pass
        assert "propagate()" in caplog.text

    def test_propagate_carries_the_context_into_a_thread(self, client: Client) -> None:
        with client.trace("root") as trace, client.span("parent"), ThreadPoolExecutor(1) as pool:
            pool.submit(evalforge.propagate(lambda: _work(client, "worker"))).result()

        assert tree(trace)["worker"] == "parent"


class TestOrphanHandling:
    def test_a_span_with_no_trace_creates_and_closes_one(self, client: Client) -> None:
        """An orphan span is more confusing than a synthetic root, and losing it is
        worse than both."""
        with client.span("lonely") as span:
            assert span is not evalforge.NOOP
            assert evalforge.current_trace() is not None
        # The implicit trace is closed and emitted, not left dangling in the context.
        assert evalforge.current_trace() is None


def _work(client: Client, name: str) -> None:
    with client.span(name):
        pass
