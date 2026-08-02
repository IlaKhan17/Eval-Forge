"""The two guarantees the SDK makes to the host application.

These are the tests that matter most. A tracing library that can crash or stall the
application it observes is worse than no tracing library, because it converts a
monitoring gap into an outage.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import evalforge
from evalforge import safety
from evalforge.client import Client
from evalforge.config import Config
from evalforge.exporter import Exporter


class TestNeverRaises:
    """With STRICT off — production behaviour."""

    @pytest.fixture(autouse=True)
    def _lenient(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(safety, "_STRICT", False)
        safety.reset_log_throttle()

    def test_a_broken_recorder_does_not_break_the_caller(self, client: Client) -> None:
        with client.trace("t") as trace, client.span("s") as span:
            # Feed it something unserializable; the SDK must absorb it.
            span.set_input(object())
            span.set_output({"nested": {"cycle": ...}})
            trace.set_metadata(weird=object())

    def test_an_exploding_snapshot_does_not_propagate(
        self, client: Client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> Any:
            msg = "snapshot exploded"
            raise RuntimeError(msg)

        monkeypatch.setattr(type(client), "emit", safety.never_raises()(boom))
        with client.trace("t"):
            pass  # must not raise

    def test_internal_failures_are_logged_once_per_window(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @safety.never_raises(key="test.site")
        def always_fails() -> None:
            msg = "nope"
            raise ValueError(msg)

        with caplog.at_level("WARNING", logger="evalforge"):
            for _ in range(100):
                always_fails()

        # One message, not one hundred. A library that logs per span during an
        # outage causes more damage than the outage.
        assert len(caplog.records) == 1

    def test_cancellation_is_never_swallowed(self) -> None:
        """CancelledError is control flow, not an error to absorb."""

        @safety.never_raises()
        def cancels() -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            cancels()

    def test_keyboard_interrupt_is_never_swallowed(self) -> None:
        @safety.never_raises()
        def interrupt() -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            interrupt()

    def test_the_noop_absorbs_everything_user_code_might_do(self) -> None:
        noop = safety.NOOP
        noop.set_input({"x": 1})
        noop.anything.at.all(1, 2, key="v")
        with noop as inner:
            inner.set_output("x")
        assert not noop

    def test_the_noop_does_not_suppress_the_callers_exception(self) -> None:
        with pytest.raises(ValueError, match="mine"), safety.NOOP:
            msg = "mine"
            raise ValueError(msg)


class TestNeverBlocks:
    def test_the_application_is_unaffected_when_the_api_is_unreachable(self) -> None:
        """The headline requirement: overhead under 5ms p99 with the API down.

        `endpoint` points at a closed port and retries are enabled, so the exporter
        is failing continuously in the background throughout.
        """
        evalforge.init(
            project="bench",
            api_key="ef_test_abcd_" + "x" * 20,
            endpoint="http://127.0.0.1:1",  # nothing is listening
            export=True,
            flush_interval_s=0.01,
            max_retries=2,
        )

        @evalforge.trace("work")
        def work(n: int) -> int:
            with evalforge.start_span("inner"):
                return n * 2

        work(1)  # warm up imports and the thread

        samples: list[float] = []
        for i in range(300):
            start = time.perf_counter()
            work(i)
            samples.append((time.perf_counter() - start) * 1000)

        samples.sort()
        p99 = samples[int(len(samples) * 0.99)]
        assert p99 < 5.0, f"p99 instrumentation overhead was {p99:.2f}ms (budget 5ms)"

    def test_submitting_to_a_full_buffer_returns_immediately(self) -> None:
        config = Config(
            project="p",
            api_key="k",
            endpoint="http://127.0.0.1:1",
            max_buffered_spans=40,  # tiny queue
        )
        exporter = Exporter(config, transport=lambda _b: time.sleep(10))
        trace = _tiny_trace()

        start = time.perf_counter()
        for _ in range(500):
            exporter.submit(trace)
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0, f"submitting to a full buffer took {elapsed:.2f}s"
        assert exporter.stats.dropped_traces > 0  # loss is counted, not silent
        exporter.shutdown(0.1)

    def test_drops_are_visible_in_the_counters(self) -> None:
        config = Config(project="p", api_key="k", max_buffered_spans=20)
        exporter = Exporter(config, transport=lambda _b: time.sleep(10))
        for _ in range(200):
            exporter.submit(_tiny_trace())
        assert exporter.stats.as_dict()["dropped_traces"] > 0
        exporter.shutdown(0.1)


class TestExceptionsPassThrough:
    def test_the_original_traceback_is_preserved(self, client: Client) -> None:
        class Domain(Exception):
            pass

        with pytest.raises(Domain, match="original") as exc_info, client.trace("t"):
            with client.span("s"):
                msg = "original"
                raise Domain(msg)

        assert exc_info.value.args == ("original",)

    def test_the_span_records_the_error(self, client: Client) -> None:
        with pytest.raises(ValueError, match="boom"), client.trace("t") as trace:
            with client.span("failing"):
                msg = "boom"
                raise ValueError(msg)

        snapshot = trace.snapshot()
        span = snapshot.spans[0]
        assert span.status.value == "error"
        assert span.error_type == "ValueError"
        assert span.status_message == "boom"

    async def test_async_exceptions_pass_through(self, client: Client) -> None:
        with pytest.raises(RuntimeError, match="async boom"):
            with client.trace("t"), client.span("s"):
                await asyncio.sleep(0)
                msg = "async boom"
                raise RuntimeError(msg)


def _tiny_trace() -> Any:
    from datetime import UTC, datetime

    from evalforge_types import Trace

    return Trace(trace_id="a" * 32, name="t", started_at=datetime.now(UTC))
