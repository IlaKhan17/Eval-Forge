#!/usr/bin/env python
"""Load driver for the targets in docs/TESTING_STRATEGY.md §8.

    uv run python tests/load/loadgen.py --endpoint http://127.0.0.1:8000 --scenario all

Requires a running API and a project API key in `EVALFORGE_API_KEY`. It writes traces, so point it
at a scratch project — `scripts/demo.sh` produces one.

## Why this and not locust

The strategy document says locust, and this is asyncio + httpx instead. The reason is that the load
here is not "fetch a URL": each request is a batch of generated spans with a realistic shape, and
the interesting numbers are per-span rather than per-request. Locust's model would have this script
generating the batches anyway, with gevent monkey-patching underneath a codebase that is
asyncio-native everywhere else. The one thing locust would add for free — a coordinated multi-worker
run — matters at the 50 000 spans/s future-scale figure, not at 2 000.

## What the numbers mean

Latency is measured **client-side**, wall clock around each request, which is what a user
experiences and is strictly pessimistic about the server. Percentiles come from the full sample, not
a reservoir. Throughput is accepted spans divided by wall clock across the whole run, so it includes
ramp-up rather than reporting a best-case window.

Every result records the machine it ran on, because a throughput number without hardware is not a
measurement. The targets in §8 are specified for 4 vCPU / 8 GiB; a run on anything else is useful
for spotting regressions and is **not** a claim about whether the target is met. `verdict` in the
output says which of the two it is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

#: The reference hardware the targets in §8 are specified for. A run anywhere else is a regression
#: check, not a pass/fail against the target.
REFERENCE = {"vcpu": 4, "memory_gib": 8}

#: Targets from docs/TESTING_STRATEGY.md §8, in the units this script measures.
TARGETS: dict[str, dict[str, float]] = {
    "ingest": {"spans_per_second": 2_000, "p95_ms": 200},
    "burst": {"spans_per_second": 10_000, "error_rate": 0.0},
    "list": {"p95_ms": 300},
    "detail": {"p95_ms": 500},
}

SPANS_PER_BATCH = 40
TIMEOUT = httpx.Timeout(60.0)


@dataclass(slots=True)
class Sample:
    """Latencies and outcomes for one scenario."""

    name: str
    latencies_ms: list[float] = field(default_factory=list)
    accepted_spans: int = 0
    errors: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def percentile(self, fraction: float) -> float | None:
        if not self.latencies_ms:
            return None
        ordered = sorted(self.latencies_ms)
        # Nearest-rank rather than interpolation: with a few hundred samples interpolation invents a
        # latency no request actually had, and p95 is meant to name an observed request.
        index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
        return round(ordered[index], 2)

    def as_dict(self) -> dict[str, Any]:
        total = len(self.latencies_ms) + self.errors
        return {
            "scenario": self.name,
            "requests": total,
            "errors": self.errors,
            "error_rate": round(self.errors / total, 4) if total else None,
            "seconds": round(self.seconds, 2),
            "accepted_spans": self.accepted_spans,
            "spans_per_second": (
                round(self.accepted_spans / self.seconds)
                if self.seconds and self.accepted_spans
                else None
            ),
            "requests_per_second": round(total / self.seconds, 1) if self.seconds else None,
            "p50_ms": self.percentile(0.50),
            "p95_ms": self.percentile(0.95),
            "p99_ms": self.percentile(0.99),
            "max_ms": round(max(self.latencies_ms), 2) if self.latencies_ms else None,
            "notes": self.notes,
        }


# ----------------------------------------------------------------------------- payloads


def _batch(run_id: str, index: int, *, spans: int = SPANS_PER_BATCH) -> dict[str, Any]:
    """One realistically-shaped batch: an agent root over LLM and tool children.

    Shape matters to the measurement. A batch of 40 identical minimal spans exercises a different
    write path from one with payloads, token counts, and a parent chain — and the second is what
    production sends.
    """
    trace_id = f"load-{run_id}-{index:06d}"
    start = datetime.now(UTC) - timedelta(seconds=spans)
    rows: list[dict[str, Any]] = []
    for position in range(spans):
        began = start + timedelta(milliseconds=position * 25)
        row: dict[str, Any] = {
            "trace_id": trace_id,
            "span_id": f"s{position:04d}",
            "parent_span_id": None if position == 0 else "s0000",
            "name": "agent" if position == 0 else f"step_{position}",
            "span_type": "agent" if position == 0 else ("llm" if position % 4 else "tool"),
            "started_at": began.isoformat(),
            "ended_at": (began + timedelta(milliseconds=22)).isoformat(),
            "sequence_index": position,
        }
        if position % 4:
            row |= {
                "model": "claude-sonnet-5",
                "provider": "anthropic",
                "tokens": {"prompt": 900, "completion": 120, "total": 1_020},
                "cost": "0.0031",
                # Under the inline limit on purpose. Offloading to object storage is a different
                # scenario with a different bottleneck, and mixing them would measure neither.
                "output": {"text": "a representative model response " * 8},
            }
        else:
            row |= {"tool_name": "search", "tool_args": {"query": "acme corp"}}
        rows.append(row)

    return {
        "resource": {"service.name": "loadgen", "environment": "production"},
        "traces": [
            {
                "trace_id": trace_id,
                "name": "agent",
                "started_at": start.isoformat(),
                "ended_at": (start + timedelta(seconds=spans)).isoformat(),
            }
        ],
        "spans": rows,
    }


# ---------------------------------------------------------------------------- scenarios


async def ingest_scenario(
    client: httpx.AsyncClient, *, name: str, batches: int, concurrency: int, run_id: str
) -> Sample:
    sample = Sample(name=name)
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(batches):
        queue.put_nowait(index)

    async def worker() -> None:
        while True:
            try:
                index = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            payload = _batch(run_id, index)
            began = time.perf_counter()
            try:
                response = await client.post("/v1/ingest/traces", json=payload)
            except Exception as exc:  # a timeout under load is a result, not a crash
                sample.errors += 1
                sample.notes.append(f"{type(exc).__name__}: {exc}"[:120])
                continue
            elapsed = (time.perf_counter() - began) * 1000
            if response.status_code >= 400:
                sample.errors += 1
                sample.notes.append(f"HTTP {response.status_code}: {response.text[:120]}")
                continue
            sample.latencies_ms.append(elapsed)
            sample.accepted_spans += int(response.json()["accepted_spans"])

    began = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    sample.seconds = time.perf_counter() - began
    # Deduplicated: 400 identical timeout notes are noise, and the count is already reported.
    sample.notes = sorted(set(sample.notes))[:5]
    return sample


async def read_scenario(
    client: httpx.AsyncClient, *, name: str, path: str, requests: int, concurrency: int
) -> Sample:
    sample = Sample(name=name)
    remaining = asyncio.Semaphore(concurrency)
    counter = iter(range(requests))

    async def worker() -> None:
        while next(counter, None) is not None:
            async with remaining:
                began = time.perf_counter()
                try:
                    response = await client.get(path)
                except Exception as exc:
                    sample.errors += 1
                    sample.notes.append(f"{type(exc).__name__}: {exc}"[:120])
                    continue
                if response.status_code >= 400:
                    sample.errors += 1
                    sample.notes.append(f"HTTP {response.status_code}")
                    continue
                sample.latencies_ms.append((time.perf_counter() - began) * 1000)

    began = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    sample.seconds = time.perf_counter() - began
    sample.notes = sorted(set(sample.notes))[:5]
    return sample


# ------------------------------------------------------------------------------ running


def _machine() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "is_reference_hardware": False,
        "reference": REFERENCE,
    }


def _verdict(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare against §8 — but only as a regression signal off reference hardware.

    Reported as `advisory` rather than pass/fail, because a run on a developer laptop that beats a
    target says nothing about a 4 vCPU box, and one that misses it may only be saying the laptop was
    busy. Committing a "PASS" from the wrong hardware is how a performance claim becomes fiction.
    """
    comparisons = {}
    for result in results:
        target = TARGETS.get(result["scenario"])
        if not target:
            continue
        comparisons[result["scenario"]] = {
            metric: {"target": limit, "measured": result.get(metric)}
            for metric, limit in target.items()
        }
    return {
        "basis": "advisory — not measured on the reference hardware in docs/TESTING_STRATEGY.md §8",
        "comparisons": comparisons,
    }


async def run(args: argparse.Namespace) -> int:
    key = os.environ.get("EVALFORGE_API_KEY")
    if not key:
        print("EVALFORGE_API_KEY is not set.", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex[:8]
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=args.endpoint.rstrip("/"),
        headers={"authorization": f"Bearer {key}"},
        timeout=TIMEOUT,
    ) as client:
        every = {"ingest", "burst", "list", "detail"}
        wanted = every if args.scenario == "all" else {args.scenario}

        if "ingest" in wanted:
            print(f"ingest: {args.batches} batches of {SPANS_PER_BATCH} spans ...")
            results.append(
                (
                    await ingest_scenario(
                        client,
                        name="ingest",
                        batches=args.batches,
                        concurrency=args.concurrency,
                        run_id=run_id,
                    )
                ).as_dict()
            )

        if "burst" in wanted:
            # Same work, four times the concurrency. The question is not throughput but whether
            # anything is *lost* — the accepted-span count must equal what was sent.
            print(f"burst: {args.batches} batches at {args.concurrency * 4}x concurrency ...")
            burst = await ingest_scenario(
                client,
                name="burst",
                batches=args.batches,
                concurrency=args.concurrency * 4,
                run_id=f"{run_id}b",
            )
            expected = args.batches * SPANS_PER_BATCH
            burst.notes.append(f"expected {expected} spans, accepted {burst.accepted_spans}")
            results.append(burst.as_dict())

        if "list" in wanted:
            print(f"list: {args.reads} requests ...")
            results.append(
                (
                    await read_scenario(
                        client,
                        name="list",
                        path="/v1/traces?limit=50",
                        requests=args.reads,
                        concurrency=args.concurrency,
                    )
                ).as_dict()
            )

        if "detail" in wanted:
            # A real trace id from this run, so the detail query walks a full 40-span tree rather
            # than a 404 path that would look impressively fast.
            listing = await client.get("/v1/traces?limit=1")
            items = listing.json().get("data") or []
            if items:
                print(f"detail: {args.reads} requests ...")
                results.append(
                    (
                        await read_scenario(
                            client,
                            name="detail",
                            path=f"/v1/traces/{items[0]['trace_id']}",
                            requests=args.reads,
                            concurrency=args.concurrency,
                        )
                    ).as_dict()
                )
            else:
                print("detail: skipped — no traces in the project")

    report = {
        "run_id": run_id,
        # Passed in rather than read from the clock inside the run, so the report is stamped once.
        "started_at": datetime.now(UTC).isoformat(),
        "endpoint": args.endpoint,
        "machine": _machine(),
        "config": {
            "batches": args.batches,
            "spans_per_batch": SPANS_PER_BATCH,
            "concurrency": args.concurrency,
            "reads": args.reads,
        },
        "results": results,
        "verdict": _verdict(results),
    }

    print()
    for result in results:
        print(
            f"  {result['scenario']:<8} "
            f"p50 {result['p50_ms']}ms  p95 {result['p95_ms']}ms  p99 {result['p99_ms']}ms  "
            f"{result['spans_per_second'] or result['requests_per_second']}/s  "
            f"errors {result['errors']}"
        )

    if args.out:
        RESULTS.mkdir(parents=True, exist_ok=True)
        target = RESULTS / args.out
        target.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nwrote {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--scenario", default="all", choices=["all", "ingest", "burst", "list", "detail"]
    )
    parser.add_argument("--batches", type=int, default=200, help="batches of 40 spans")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--reads", type=int, default=200)
    parser.add_argument("--out", default=None, help="filename under tests/load/results/")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
