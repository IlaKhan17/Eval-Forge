"""Batching exporter with a bounded buffer.

The contract with the host application, in priority order:

1. **Never block.** Submitting a trace is a non-blocking enqueue. If the buffer is
   full we drop and count; a full buffer must never become backpressure on the
   caller's request path.
2. **Never raise.** Every failure is swallowed and logged once per window.
3. **Lose visibly.** Drops increment a counter that is reported on the trace itself.
   Silent loss is worse than visible loss, because it looks like the workflow simply
   did not do the thing.

A background thread rather than an asyncio task: the SDK must work identically in a
sync Django view, a Celery worker, and an async FastAPI handler, and a thread is the
only one of those that needs no event loop.
"""

from __future__ import annotations

import gzip
import json
import queue
import random
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from proofstep.safety import log_once, never_raises

if TYPE_CHECKING:
    from proofstep.config import Config
    from proofstep_types import Trace

USER_AGENT = "proofstep-python"


class ExportStats:
    """Observable counters. A telemetry client that cannot report its own health is
    the one thing worse than no telemetry."""

    __slots__ = ("dropped_traces", "exported_spans", "exported_traces", "failures", "spooled")

    def __init__(self) -> None:
        self.exported_traces = 0
        self.exported_spans = 0
        self.dropped_traces = 0
        self.failures = 0
        self.spooled = 0

    def as_dict(self) -> dict[str, int]:
        return {slot: getattr(self, slot) for slot in self.__slots__}


class Exporter:
    def __init__(self, config: Config, *, transport: Any = None) -> None:
        self.config = config
        self.stats = ExportStats()
        self._queue: queue.Queue[Trace | None] = queue.Queue(maxsize=_capacity(config))
        self._transport = transport
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            # Daemon: a forgotten flush() must never stop the process exiting.
            # `atexit` handles the orderly case.
            self._thread = threading.Thread(
                target=self._run, name="proofstep-exporter", daemon=True
            )
            self._thread.start()

    @never_raises()
    def submit(self, trace: Trace) -> None:
        """Non-blocking enqueue. Drops the oldest item when full."""
        if not self.config.sends:
            return
        self._ensure_started()
        self._idle.clear()
        try:
            self._queue.put_nowait(trace)
        except queue.Full:
            # Drop the oldest: the newest trace is the one someone is probably
            # watching for, and an unbounded queue is how a telemetry client turns
            # a provider outage into an OOM.
            try:
                self._queue.get_nowait()
                self.stats.dropped_traces += 1
                self._queue.put_nowait(trace)
            except (queue.Empty, queue.Full):
                self.stats.dropped_traces += 1
            log_once(
                "exporter.full",
                f"export buffer is full ({self._queue.maxsize} traces); dropping oldest. "
                f"{self.stats.dropped_traces} dropped so far.",
            )

    @never_raises(default=False)
    def flush(self, timeout: float | None = None) -> bool:
        """Wait for the queue to drain. Returns False on timeout."""
        if self._thread is None or not self._thread.is_alive():
            return True
        deadline = time.monotonic() + (
            timeout if timeout is not None else self.config.shutdown_timeout_s
        )
        while time.monotonic() < deadline:
            if self._queue.empty() and self._idle.is_set():
                return True
            time.sleep(0.01)
        log_once("exporter.flush_timeout", "flush timed out; some traces were not sent")
        return False

    @never_raises()
    def shutdown(self, timeout: float | None = None) -> None:
        self.flush(timeout)
        self._stopping.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            self._queue.put_nowait(None)  # wake the worker
            thread.join(timeout=1.0)

    # --------------------------------------------------------------------- worker

    def _run(self) -> None:
        batch: list[Trace] = []
        last_flush = time.monotonic()

        while not self._stopping.is_set():
            timeout = max(0.01, self.config.flush_interval_s - (time.monotonic() - last_flush))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None
            else:
                if item is None:
                    break
                batch.append(item)

            due = (time.monotonic() - last_flush) >= self.config.flush_interval_s
            spans = sum(len(t.spans) for t in batch)
            if batch and (due or spans >= self.config.batch_size):
                self._send_batch(batch)
                batch = []
                last_flush = time.monotonic()

            if not batch and self._queue.empty():
                self._idle.set()

        if batch:
            self._send_batch(batch)
        self._idle.set()

    def _send_batch(self, batch: list[Trace]) -> None:
        payload = _encode(batch, self.config)
        if payload is None:
            return

        body = gzip.compress(payload)
        for attempt in range(self.config.max_retries):
            if self._stopping.is_set() and attempt > 0:
                break
            try:
                self._post(body)
            except Exception as exc:
                self.stats.failures += 1
                if attempt == self.config.max_retries - 1:
                    log_once(
                        "exporter.send_failed",
                        f"could not reach {self.config.endpoint} after "
                        f"{self.config.max_retries} attempts ({type(exc).__name__}: {exc}); "
                        "traces are being spooled or dropped",
                    )
                    self._spool(body)
                    return
                # Exponential backoff with full jitter: synchronized retries from
                # many processes are how a struggling server stays down.
                delay = min(30.0, (2**attempt) * 0.5)
                time.sleep(random.uniform(0, delay))  # noqa: S311 — jitter, not crypto
            else:
                self.stats.exported_traces += len(batch)
                self.stats.exported_spans += sum(len(t.spans) for t in batch)
                return

    def _post(self, body: bytes) -> None:
        if self._transport is not None:
            self._transport(body)
            return

        import httpx  # noqa: PLC0415 — lazy so importing the SDK costs nothing

        response = httpx.post(
            f"{self.config.endpoint}/v1/ingest/traces",
            content=body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
                "User-Agent": USER_AGENT,
            },
            timeout=self.config.export_timeout_s,
        )
        if response.status_code >= 500 or response.status_code == 429:
            msg = f"server returned {response.status_code}"
            raise RuntimeError(msg)
        if response.status_code >= 400:
            # 4xx is our bug or a bad key. Retrying cannot fix it, and retrying an
            # auth failure five times per batch is how you get rate-limited too.
            log_once(
                "exporter.rejected",
                f"ingest rejected the batch with {response.status_code}: {response.text[:200]}",
            )

    def _spool(self, body: bytes) -> None:
        if self.config.spool_dir is None:
            return
        try:
            directory = Path(self.config.spool_dir)
            directory.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}-{random.randbytes(4).hex()}.json.gz"  # noqa: S311
            (directory / name).write_bytes(body)
            self.stats.spooled += 1
        except OSError as exc:
            log_once("exporter.spool_failed", f"could not spool batch: {exc}")


def _capacity(config: Config) -> int:
    """Queue length in traces, derived from the span budget."""
    return max(16, config.max_buffered_spans // 20)


def _encode(batch: list[Trace], config: Config) -> bytes | None:
    traces: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for trace in batch:
        dumped = trace.model_dump(mode="json", exclude={"spans"})
        traces.append(dumped)
        for span in trace.spans:
            spans.append(span.model_dump(mode="json"))

    payload = {
        "resource": {
            "service.name": config.service_name or config.project or "unknown",
            "environment": config.environment,
            "git.commit": config.git_commit,
            "sdk.name": USER_AGENT,
        },
        "traces": traces,
        "spans": spans,
        "dropped_span_count": sum(t.dropped_span_count for t in batch),
    }

    try:
        encoded = json.dumps(payload, default=str).encode("utf-8")
    except (TypeError, ValueError) as exc:
        log_once("exporter.encode_failed", f"could not serialize batch: {exc}")
        return None

    if len(encoded) > config.max_batch_bytes:
        log_once(
            "exporter.batch_too_large",
            f"batch of {len(encoded)} bytes exceeds max_batch_bytes "
            f"({config.max_batch_bytes}); dropping it",
        )
        return None
    return encoded
