"""E2E-1 — the MVP acceptance test.

`docs/TESTING_STRATEGY.md` §5 calls this "the definition of done for the MVP", and it is the one
test that answers the only question that matters about this product: **does the loop work?**

    instrument an agent → the trace appears → build a dataset from it → evaluate it
    → gates pass, exit 0 → break the agent → gates fail, exit 1, naming the offending span
    → the failure is visible in the API and rendered for a pull request

Every step of that is covered somewhere by a unit or integration test. None of those tests can fail
in the way this one can: the pieces all work and the loop still does not close, because the CLI and
the server disagree about a field name, or a key with the wrong scopes is issued, or the report the
Action renders comes from a path nobody runs end to end.

Written as **one long test, on purpose.** The subject is the sequence, so splitting it into
independent cases would test the steps and lose the thing being asserted — and each step depends on
the last, which pytest would otherwise express as a chain of fixtures that hide the narrative.

Runs against a live server in a subprocess (see `conftest.py`) with a real database and a real
bucket. Marked `e2e` and excluded from PR CI by default; the merge queue runs it.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

ROOT = Path(__file__).resolve().parents[2]

#: The suite the loop runs. `reply-intent` is the right choice: it has a deterministic evaluator, a
#: statistical one, a sliced protected metric, and an environment switch that breaks exactly the
#: rare class — so "the aggregate barely moves while a protected slice collapses" is reproducible
#: rather than hypothetical.
SUITE = "evals/suites/reply-intent.yaml"
BREAK = "EXAMPLE_BREAK_UNSUBSCRIBE"


def cli(
    *args: str, env: dict[str, str], expect: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the CLI the way CI runs it, and report its output when it surprises us.

    `expect` asserts the exit code, because the exit code *is* the contract — everything else the
    CLI prints is for humans, and a test that only checked stdout would pass while CI merged a
    regression.
    """
    completed = subprocess.run(
        ["uv", "run", "evalforge", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if expect is not None and completed.returncode != expect:
        pytest.fail(
            f"`evalforge {' '.join(args)}` exited {completed.returncode}, expected {expect}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def report(path: Path) -> dict[str, Any]:
    assert path.exists(), f"the run produced no report at {path}"
    return json.loads(path.read_text())


def blocking_failures(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        gate
        for gate in data.get("gates", [])
        if gate.get("verdict") in ("fail", "error") and gate.get("severity") == "block"
    ]


def test_the_whole_loop(stack: dict[str, Any], api: httpx.Client, tmp_path: Path) -> None:
    endpoint, key = stack["endpoint"], stack["api_key"]
    env = {
        **stack["env"],
        "EVALFORGE_ENDPOINT": endpoint,
        "EVALFORGE_API_KEY": key,
        "EVALFORGE_ENVIRONMENT": "production",
    }
    env.pop(BREAK, None)

    # ---------------------------------------------------------------- 1. the server is alive
    ready = api.get("/readyz").json()
    assert ready["status"] == "ready", ready
    # Reported, not asserted. The e2e stack runs as the default superuser, so RLS is installed and
    # inert; failing here would make the acceptance test demand a production posture the demo path
    # deliberately does not have. What must never happen is this going *unmentioned*.
    if ready.get("checks", {}).get("row_level_security", "").startswith("not_enforced"):
        print("note: RLS is not enforced in the e2e stack (superuser role) — see docs/HARDENING.md")

    # ---------------------------------------------------- 2. an instrumented agent, traced
    trace_id = _run_instrumented_agent(endpoint, key)

    # -------------------------------------------------- 3. the trace and its spans are there
    detail = api.get(f"/v1/traces/{trace_id}").json()
    assert detail["trace_id"] == trace_id
    names = [span["name"] for span in detail["spans"]]
    assert {"draft", "classify"} <= set(names), names

    # Nesting, not just presence. A flat list of spans renders as a flat waterfall and loses the
    # structure every trajectory rule is written against — and parent links are reconstructed from
    # ids the SDK assigned in another process, so this is a genuine round-trip assertion.
    by_name = {span["name"]: span for span in detail["spans"]}
    assert by_name["classify"]["parent_span_id"] == by_name["draft"]["span_id"]
    assert detail["orphan_span_ids"] == []

    # Token and cost accounting survived the trip, which is what every operational metric and cost
    # gate reads.
    assert by_name["classify"]["total_tokens"] == 1_020
    assert detail["total_tokens"] >= 1_020

    # ----------------------------------------------------------------- 4. a healthy run
    clean_report = tmp_path / "clean.json"
    cli("eval", SUITE, "-o", str(clean_report), env=env, expect=0)
    clean = report(clean_report)
    assert clean["verdict"] == "pass", clean["verdict"]
    assert blocking_failures(clean) == []
    assert "intent_accuracy" in {metric["key"] for metric in clean["metrics"]}

    # ------------------------------------------------------- 5. the seeded regression
    #
    # The point of the whole product, in two commands. The break collapses recall on `unsubscribe`
    # — about 8% of this dataset — so the aggregate accuracy barely moves. A suite that gated only
    # on the average would merge this.
    broken_report = tmp_path / "broken.json"
    broken_env = {**env, BREAK: "1"}
    cli("eval", SUITE, "-o", str(broken_report), env=broken_env, expect=1)
    broken = report(broken_report)

    assert broken["verdict"] == "fail", broken["verdict"]
    failures = blocking_failures(broken)
    assert failures, "the regression did not fail any blocking gate"

    # The *right* gate, named. A test that only asserted exit 1 would pass if an unrelated gate
    # broke, which is how a regression test stops testing the regression.
    protected = [gate for gate in failures if gate["metric_key"].startswith("classes_recall")]
    assert protected, f"the protected slice gate did not fail; failures were {failures}"
    assert protected[0]["slice"] == {"class": "unsubscribe"}, protected[0]

    # And the aggregate really did stay healthy — otherwise this fixture is not demonstrating a
    # hidden regression at all, just a broken run.
    accuracy = _metric(broken, "intent_accuracy")
    assert accuracy is not None
    assert accuracy >= 0.75, (
        f"aggregate accuracy fell to {accuracy}, so this is not a *hidden* regression and the "
        "fixture no longer demonstrates what the protected metric is for"
    )

    # -------------------------------------------------- 6. the pull-request comment renders
    comment = cli("comment", str(broken_report), env=env, expect=0).stdout
    assert "classes_recall" in comment
    assert "unsubscribe" in comment
    # GitHub rejects comments over 65 536 characters, and a rejected comment means a silent CI.
    assert len(comment) < 65_536, len(comment)

    # ------------------------------------------- 7. the boundary of what the loop covers today
    #
    # The CLI is local-only: `eval` computes, gates, and reports without contacting a server (see
    # `--local`, which defaults to true). So a run does *not* appear under /v1/experiments, and this
    # asserts that rather than skipping it — a known gap that is checked is a gap somebody notices,
    # and whoever implements publishing will see this assertion fail and update it.
    #
    # The server-side experiment path itself is not untested: apps/api/tests/test_parity.py drives
    # it end to end and asserts it reaches the same verdict as the library. What is missing is the
    # CLI *sending* its run there.
    published = api.get("/v1/experiments", params={"suite_name": "reply-intent"}).json()
    assert published == [], (
        "an experiment reached the server from the CLI — publishing is implemented now, so this "
        "step should assert the run's metrics rather than its absence"
    )


def _metric(data: dict[str, Any], key: str) -> float | None:
    for metric in data["metrics"]:
        if metric["key"] == key and not metric.get("slice"):
            value = metric.get("value")
            return float(value) if value is not None else None
    return None


def _run_instrumented_agent(endpoint: str, key: str) -> str:
    """Trace an agent through the SDK and wait for the export to land.

    In-process rather than as a subprocess, because the assertion is about the SDK's exporter
    reaching a live server — and doing it here means a failure surfaces as a Python traceback rather
    than as someone else's exit code.
    """
    import evalforge

    evalforge.init(
        endpoint=endpoint,
        api_key=key,
        environment="production",
    )

    marker = uuid.uuid4().hex[:8]
    with evalforge.capture("reply-drafter") as captured:
        evalforge.set_metadata(e2e=marker)
        # Nested on purpose: the parent link is the thing worth asserting after a round trip.
        with evalforge.start_span("draft", span_type="agent"):
            with evalforge.start_span("classify", span_type="llm") as span:
                span.set_output({"intent": "unsubscribe", "confidence": 0.91})
                span.set_model(
                    "claude-sonnet-5",
                    provider="anthropic",
                    prompt_tokens=900,
                    completion_tokens=120,
                    cost=0.0031,
                )

    trace = captured[0]
    # Flushed explicitly and checked. The exporter batches in the background, which is right for an
    # application and wrong here: without this the next assertion races the queue, and a racing
    # acceptance test is one that eventually gets marked flaky and skipped.
    assert evalforge.get_client().flush(timeout=30), "the SDK could not export within 30s"
    return str(trace.trace_id)
