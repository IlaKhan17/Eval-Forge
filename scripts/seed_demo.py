#!/usr/bin/env python
"""Seed a running EvalForge with traces, an online rule, and a populated review queue.

Called by `scripts/demo.sh`, but usable on its own against any project:

    EVALFORGE_API_KEY=ef_dev_... EVALFORGE_ENDPOINT=http://127.0.0.1:8000 \
        uv run python scripts/seed_demo.py

Everything goes through the public HTTP API with an ordinary project API key — no direct
database access. That is the point: if the seeder can do it, so can a user's own script,
and a demo that reached behind the API could hide a broken endpoint.

The traces are Davis-shaped because that is the reference example, and Davis is an
*example* here rather than product logic: nothing the server does knows what a
"suppression list" is. The trajectory policy is data, supplied below from
`evals/policies/davis-agent-policy.yaml`.

Idempotent. Rules, queues, and policies are looked up by slug and reused; spans are
deduplicated by `(trace_id, span_id)` server-side. Re-running adds a fresh round of
traces with new ids, which is usually what you want from a demo.
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "evals" / "policies" / "davis-agent-policy.yaml"

#: How many traces to seed. Enough that the list view paginates, the latency chart has a
#: shape, and the failure rate is a rate rather than a single anecdote — and small enough
#: that the whole demo is under a minute.
TRACE_COUNT = 60

#: Every Nth trace violates the policy — 1 in 7, so about 14%. Not 50%: a demo where half of
#: production is broken teaches nothing about finding the few that are, which is the actual problem.
#:
#: A count rather than a probability, so the number printed at the end is the number seeded.
#: Thresholding a hash was the first version and it drifted badly at this sample size — a stated
#: 15% produced 17 of 60, and a seeder that misreports its own data is the last thing you want when
#: you are using it to decide whether the *product* is reporting correctly.
VIOLATION_EVERY = 7

TIMEOUT = httpx.Timeout(30.0)


class Api:
    """Thin wrapper that fails loudly.

    A seeder that swallowed a 4xx would leave a half-populated demo and no explanation,
    which is worse than not running.
    """

    def __init__(self, base: str, key: str) -> None:
        self._client = httpx.Client(
            base_url=base.rstrip("/"),
            headers={"authorization": f"Bearer {key}"},
            timeout=TIMEOUT,
        )

    def __enter__(self) -> Api:
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            sys.exit(f"{method} {path} → {response.status_code}: {response.text[:400]}")
        return response.json() if response.content else None

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)


# --------------------------------------------------------------------------- trace shapes


def _clock(index: int) -> datetime:
    """Spread traces over the last few hours.

    Backwards from now rather than from a fixed date, so the dashboard's default "last 24
    hours" window shows them. A seeder that wrote timestamps outside the default filter
    produces an empty dashboard and a bug report.
    """
    return datetime.now(UTC) - timedelta(minutes=7 * index + 3)


def _bucket(index: int) -> float:
    """Deterministic pseudo-randomness, so two runs of the demo look the same.

    `random` would work and would make the seeded data differ run to run, which makes
    "is this the bug or the seed?" harder to answer during a demo.
    """
    digest = hashlib.sha256(f"seed-{index}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def _spans(trace_id: str, start: datetime, *, violating: bool, index: int) -> list[dict[str, Any]]:
    """The tool sequence Davis actually emits, or the shortcut version.

    Mirrors `examples/davis_sdr/agent.py` rather than importing it: the agent traces itself
    through the SDK's context managers, and reproducing that here as literal spans keeps the
    seeder to one HTTP call per batch and independent of the SDK's exporter.
    """
    offset = 0
    spans: list[dict[str, Any]] = []

    def add(name: str, span_type: str = "tool", **extra: Any) -> None:
        nonlocal offset
        began = start + timedelta(milliseconds=offset)
        # Varied but deterministic durations, so the waterfall and the p95 chart are not flat.
        took = 40 + int(_bucket(index + len(spans)) * 900)
        offset += took + 5
        spans.append(
            {
                "trace_id": trace_id,
                "span_id": f"s{len(spans):02d}",
                "parent_span_id": None if not spans else "s00",
                "name": name,
                "span_type": span_type,
                "started_at": began.isoformat(),
                "ended_at": (began + timedelta(milliseconds=took)).isoformat(),
                "sequence_index": len(spans),
                **extra,
            }
        )

    add("davis.outbound", "agent")
    for _ in range(2):
        add("web_search", tool_name="web_search", output={"hits": 3})
    add(
        "generate_email",
        "llm",
        model="claude-sonnet-5",
        provider="anthropic",
        tokens={"prompt": 1_840, "completion": 260, "total": 2_100},
        cost="0.0072",
        output={"subject": "Following up on your pipeline review"},
    )

    if violating:
        # The whole point of the demo's failing traces: the email is fine, the behaviour is
        # not. No injection scan, no claim validation, no approval — and one duplicate send.
        for copy in range(2):
            add(
                "gmail.send",
                tool_name="gmail.send",
                tool_args={"to": "buyer@example.com", "thread_id": "t-9", "suppressed": False},
                output={"copy": copy},
            )
        return spans

    add(
        "guardrail.injection_scan",
        "guardrail",
        tool_name="guardrail.injection_scan",
        output={"clean": True},
    )
    add("validate_claims", "guardrail", tool_name="validate_claims", output={"unsupported": 0})
    add("request_approval", tool_name="request_approval")
    add("approval_received", tool_name="approval_received", output={"approver": "dana"})
    add(
        "gmail.send",
        tool_name="gmail.send",
        tool_args={
            "to": f"buyer{index}@example.com",
            "thread_id": f"t-{index}",
            "suppressed": False,
        },
    )
    return spans


def _batch(index: int, *, run_tag: str) -> dict[str, Any]:
    violating = index % VIOLATION_EVERY == 0
    start = _clock(index)
    trace_id = f"demo-{run_tag}-{index:03d}"
    spans = _spans(trace_id, start, violating=violating, index=index)
    last_end = max(str(span["ended_at"]) for span in spans)
    return {
        "resource": {"service.name": "davis-sdr", "environment": "production"},
        "traces": [
            {
                "trace_id": trace_id,
                "name": "davis.outbound",
                "started_at": start.isoformat(),
                "ended_at": last_end,
                "status": "ok",
                # The trajectory policy's `no-send-after-unsubscribe` rule reads
                # `state.unsubscribed`; a rule cannot check what the trace does not carry.
                "state": {"unsubscribed": False},
                "metadata": {"scenario": "violating" if violating else "clean", "seeded": True},
                "session_id": f"sess-{index // 6}",
            }
        ],
        "spans": spans,
    }


# ------------------------------------------------------------------------------ seeding


def seed_traces(api: Api, *, run_tag: str) -> tuple[int, int]:
    accepted = 0
    violating = 0
    for index in range(TRACE_COUNT):
        payload = _batch(index, run_tag=run_tag)
        violating += payload["traces"][0]["metadata"]["scenario"] == "violating"
        result = api.post("/v1/ingest/traces", json=payload)
        accepted += int(result["accepted_traces"])
        if result["rejected"]:
            # Reported rather than ignored. A silently partial seed is the kind of thing that
            # gets diagnosed as a query bug an hour later.
            print(f"  ! {len(result['rejected'])} item(s) rejected: {result['rejected'][0]}")
    return accepted, violating


def seed_policy(api: Api) -> str:
    policy = api.post(
        "/v1/trajectory-policies",
        json={
            "name": "Davis outbound policy",
            "slug": "davis-agent-policy",
            "description": "Reference trajectory policy — see evals/policies/.",
        },
    )
    version = api.post(
        f"/v1/trajectory-policies/{policy['id']}/versions",
        json={"source_yaml": POLICY_PATH.read_text()},
    )
    return str(version["id"])


def seed_queue(api: Api) -> str:
    for queue in api.get("/v1/review-queues"):
        if queue["slug"] == "policy-failures":
            return str(queue["id"])
    created = api.post(
        "/v1/review-queues",
        json={
            "name": "Policy failures",
            "slug": "policy-failures",
            "description": "Traces an online trajectory rule flagged, awaiting a human verdict.",
            "lease_seconds": 900,
        },
    )
    return str(created["id"])


def seed_rule(api: Api, *, policy_version_id: str, queue_id: str) -> str:
    for rule in api.get("/v1/online-rules"):
        if rule["slug"] == "davis-policy-all-traces":
            return str(rule["id"])
    created = api.post(
        "/v1/online-rules",
        json={
            "name": "Davis policy on every trace",
            "slug": "davis-policy-all-traces",
            "kind": "trajectory",
            "policy_version_id": policy_version_id,
            # 100%, because a deterministic trajectory policy costs nothing per trace. The
            # sampling machinery exists for judges, which do.
            "sample_rate": 1.0,
            "escalate_on_failure": True,
            "review_queue_id": queue_id,
            "trace_name": "davis.outbound",
        },
    )
    return str(created["id"])


def main() -> int:
    key = os.environ.get("EVALFORGE_API_KEY")
    if not key:
        print("EVALFORGE_API_KEY is not set.", file=sys.stderr)
        return 2
    base = os.environ.get("EVALFORGE_ENDPOINT", "http://127.0.0.1:8000")
    # Distinguishes this run's traces from a previous run's, so re-seeding adds rather than
    # collides. Second-resolution is enough: two runs inside one second would be the same demo.
    run_tag = datetime.now(UTC).strftime("%H%M%S")

    with Api(base, key) as api:
        traces, violating = seed_traces(api, run_tag=run_tag)
        print(f"  seeded {traces} traces, {violating} of which violate the policy")

        policy_version_id = seed_policy(api)
        queue_id = seed_queue(api)
        seed_rule(api, policy_version_id=policy_version_id, queue_id=queue_id)
        print("  registered the trajectory policy, the review queue, and an online rule")

        # Run the rule now rather than waiting for the worker's cron, so the demo has
        # evaluations and a populated queue the moment it finishes printing.
        outcome = api.post("/v1/online-rules/run", params={"limit": 500, "hours": 24})
        print(
            f"  evaluated {outcome['evaluations_written']} traces online: "
            f"{outcome['failures']} failed, {outcome['queued_for_review']} queued for review"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
