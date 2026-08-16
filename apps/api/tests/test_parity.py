"""The same verdict everywhere.

This file tests the claim the product rests on: **the exit code CI acts on and the verdict the
dashboard shows are the same verdict.** They are produced in different processes, from different
representations of the same data — in-memory `ExampleResult` objects on one side, rows rehydrated
out of Postgres on the other — and reached through different call stacks. Nothing but this suite
keeps them identical.

The way they drift is not a wrong answer. It is one side quietly acquiring a special case: an
aggregate recomputed in SQL "for speed" that forgets errored scores are excluded from the mean,
a slice matched by a stored `slice_key` string that disagrees with a tuple comparison, a missing
metric treated as absent on one side and as zero on the other. Each of those is a one-line change
that passes every existing test.

So the assertion is **byte equality of the normalised report**, not "both blocked". Two sides can
agree on the verdict and disagree about which rule produced it, which is the same bug one step
further from being noticed.

Fixtures live in `tests/fixtures/parity/`, one JSON file per case, deliberately chosen for places
the two paths *could* differ. `tests/fixtures/parity/README.md` explains the format.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from factories import Tenant
from httpx import ASGITransport, AsyncClient
from proofstep_api.api.dependencies import get_session
from proofstep_api.main import create_app
from proofstep_api.settings import Settings
from sqlalchemy.ext.asyncio import AsyncSession

from proofstep_core.aggregate import aggregate_scores
from proofstep_core.gates import evaluate_gates
from proofstep_trajectory import evaluate_policy, load_policy
from proofstep_types import ExampleResult, GateRule, GateSet, Span, Trace

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "parity"


def cases() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32"))

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


def auth(tenant: Tenant) -> dict[str, str]:
    return {"authorization": f"Bearer {tenant.token}"}


# --------------------------------------------------------------------------- normalising


def normalise_gate(result: dict[str, Any]) -> dict[str, Any]:
    """One comparable shape for a gate result, from either side.

    Only the fields that carry meaning: which rule, on which metric and slice, reached what verdict
    against what number. Deliberately excluded is the human-readable `message` — it is prose, it is
    rendered from the same code on both sides anyway, and including it would make this suite fail on
    a wording change, which is the fastest way to get a parity suite deleted.
    """
    return {
        "metric_key": result["metric_key"],
        "slice": result.get("slice") or None,
        "verdict": result["verdict"],
        "severity": result["severity"],
        "rule": result["rule"],
        "threshold": _round(result.get("threshold")),
        "actual": _round(result.get("actual")),
        "baseline": _round(result.get("baseline")),
    }


def _round(value: Any) -> Any:
    """Round floats before comparing.

    Not laziness about correctness — a genuine representation difference. The library holds a Python
    float; the server round-trips it through Postgres `double precision` and back through JSON.
    Nine decimal places is far tighter than any threshold anyone gates on and immune to the last-bit
    difference that round trip can introduce.
    """
    return round(value, 9) if isinstance(value, float) else value


def sort_key(gate: dict[str, Any]) -> tuple[str, str, str]:
    return (gate["metric_key"], json.dumps(gate["slice"], sort_keys=True), gate["rule"])


def normalise_report(verdict: str, exit_code: int, gates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "exit_code": exit_code,
        "gates": sorted((normalise_gate(g) for g in gates), key=sort_key),
    }


# ------------------------------------------------------------------------------- the two paths


def library_report(case: dict[str, Any]) -> dict[str, Any]:
    """What the CLI would produce, in process, with no server involved."""
    gate_set = GateSet.model_validate(case["gate_set"])
    candidate = aggregate_scores(
        [ExampleResult.model_validate(r) for r in case["candidate"]], confidence_intervals=True
    )
    baseline = (
        aggregate_scores(
            [ExampleResult.model_validate(r) for r in case["baseline"]], confidence_intervals=True
        )
        if case.get("baseline")
        else None
    )
    report = evaluate_gates(gate_set, candidate, baseline)
    return normalise_report(
        report.verdict.value,
        report.exit_code,
        [
            {
                "metric_key": r.metric_key,
                "slice": r.slice,
                "verdict": r.verdict,
                "severity": r.severity.value,
                "rule": r.rule,
                "threshold": r.threshold,
                "actual": r.actual,
                "baseline": r.baseline,
            }
            for r in report.results
        ],
    )


async def server_report(
    client: AsyncClient, tenant: Tenant, case: dict[str, Any]
) -> dict[str, Any]:
    """What the API produces for the same inputs, through its own storage and gate path.

    Everything goes through the public HTTP API — no direct calls into the service layer. Reaching
    into the service would skip serialisation, one of the two places a divergence can hide.
    """
    head = auth(tenant)
    suite = f"parity-{uuid.uuid4().hex[:8]}"

    dataset = (
        await client.post(
            "/v1/datasets", headers=head, json={"name": suite, "slug": suite, "kind": "golden"}
        )
    ).json()
    version = (
        await client.post(
            f"/v1/datasets/{dataset['id']}/versions", headers=head, json={"version": "v1"}
        )
    ).json()

    # The dataset has to hold the examples the results refer to, and be locked, because an
    # experiment refuses a draft version. That refusal is the reproducibility guarantee, so the test
    # satisfies it rather than working around it.
    example_ids = sorted(
        {r["example_id"] for r in case["candidate"]}
        | {r["example_id"] for r in case.get("baseline") or []}
    )
    await client.post(
        f"/v1/dataset-versions/{version['id']}/examples",
        headers=head,
        json={"examples": [{"id": eid, "input": {"i": eid}} for eid in example_ids]},
    )
    await client.post(f"/v1/dataset-versions/{version['id']}/lock", headers=head, json={})

    gate_set = (
        await client.post(
            "/v1/quality-gate-sets",
            headers=head,
            json={
                "name": case["gate_set"].get("name", "default"),
                "rules": case["gate_set"]["rules"],
                "require_dataset_match": case["gate_set"].get("require_dataset_match", True),
            },
        )
    ).json()

    experiment = (
        await client.post(
            "/v1/experiments",
            headers=head,
            json={
                "name": suite,
                "suite_name": suite,
                "dataset_version_id": version["id"],
                "git_branch": "main",
            },
        )
    ).json()

    baseline_run_id = None
    if case.get("baseline"):
        baseline_run_id = await _run(client, head, experiment["id"], case["baseline"])
    candidate_run_id = await _run(client, head, experiment["id"], case["candidate"])

    compared = (
        await client.post(
            "/v1/experiments/compare",
            headers=head,
            json={
                "candidate_run_id": candidate_run_id,
                "baseline_run_id": baseline_run_id,
                "gate_set_id": gate_set["id"],
            },
        )
    ).json()
    return normalise_report(compared["verdict"], compared["exit_code"], compared["gates"])


async def _run(
    client: AsyncClient, head: dict[str, str], experiment_id: str, results: list[dict[str, Any]]
) -> str:
    run = (await client.post(f"/v1/experiments/{experiment_id}/runs", headers=head, json={})).json()
    await client.post(
        f"/v1/experiment-runs/{run['id']}/results", headers=head, json={"results": results}
    )
    # Completing triggers server-side aggregation, which is half of what this suite compares.
    await client.post(
        f"/v1/experiment-runs/{run['id']}/complete", headers=head, json={"status": "succeeded"}
    )
    return str(run["id"])


# ------------------------------------------------------------------------------------ the tests


@pytest.mark.parametrize("path", cases(), ids=lambda p: p.stem)
async def test_the_library_and_the_api_reach_the_same_verdict(
    path: Path, client: AsyncClient, tenant_a: Tenant
) -> None:
    case = load(path)
    expected = library_report(case)
    actual = await server_report(client, tenant_a, case)
    assert actual == expected, (
        f"{path.name}: the CLI and the API disagree.\n"
        f"why this case exists: {case.get('why', '(no reason recorded)')}\n"
        f"library: {json.dumps(expected, indent=2)}\n"
        f"server:  {json.dumps(actual, indent=2)}"
    )


def test_there_are_fixtures() -> None:
    """A parity suite with no fixtures passes silently and proves nothing.

    Worth its own test because the fixtures are discovered from a directory: a bad path, a renamed
    folder, or a packaging change that stops shipping them turns this whole file into a no-op that
    reports green.
    """
    assert len(cases()) >= 5, f"expected the parity fixtures in {FIXTURES}, found {len(cases())}"


def test_every_case_records_why_it_exists() -> None:
    # A fixture nobody can explain is a fixture nobody will maintain, and the first one to fail
    # spuriously gets deleted rather than understood.
    missing = [path.name for path in cases() if not load(path).get("why")]
    assert not missing, f"these parity cases have no `why`: {missing}"


def test_the_wire_model_can_express_every_gate_rule_field() -> None:
    """Structural guard, so the next dropped field fails here rather than in production.

    The fixtures above catch a divergence only if some case happens to exercise the lost field.
    This catches it the moment someone adds a field to `GateRule` with no wire representation —
    which is how `severity` and `max_error_rate` became silently unrepresentable, turning every
    `warn` rule into a blocking one on the server.
    """
    from proofstep_api.api.routes.evaluation import GateRuleIn

    shared = set(GateRule.model_fields)
    wire = set(GateRuleIn.model_fields)
    # `blocking` is the legacy boolean form of `severity`: extra on the wire, not missing here.
    missing = shared - wire
    assert not missing, (
        f"GateRule fields with no wire representation: {sorted(missing)}. "
        "Add them to GateRuleIn and pass them through in create_gate_set, or the server stores a "
        "different rule from the one the repository declared."
    )


# ------------------------------------------------------------------- trajectory parity

TRAJECTORY = FIXTURES / "trajectory"

#: Recent, not a fixed date. Online evaluation only reaches back a bounded window — deliberately, so
#: a restarted worker cannot replay a month of traffic through judges — and a trace stamped in the
#: past is correctly ignored. Computed once at import so both sides of the comparison use the same
#: instant.
BASE = datetime.now(UTC) - timedelta(minutes=5)


def trajectory_cases() -> list[Path]:
    return sorted(TRAJECTORY.glob("*.json"))


def domain_trace(case: dict[str, Any], trace_id: str) -> Trace:
    """Build the trace the engine sees, independently of the server.

    Constructed here rather than by reusing `OnlineEvalService.load_trace`, which would make the
    comparison circular: the point is that a trace rebuilt from rows behaves like the trace the SDK
    captured, so the two constructions have to be independent.
    """
    return Trace(
        trace_id=trace_id,
        name="parity",
        started_at=BASE,
        state=case.get("state") or {},
        metadata=case.get("metadata") or {},
        spans=[
            Span(
                trace_id=trace_id,
                span_id=row["span_id"],
                name=row["name"],
                span_type=row["span_type"],
                tool_name=row.get("tool_name"),
                tool_args=row.get("tool_args"),
                started_at=BASE + timedelta(milliseconds=row["offset_ms"]),
                ended_at=BASE + timedelta(milliseconds=row["offset_ms"] + 5),
                sequence_index=index,
            )
            for index, row in enumerate(case["spans"])
        ],
    )


def engine_failures(case: dict[str, Any]) -> list[dict[str, Any]]:
    result = evaluate_policy(load_policy(case["policy_yaml"]), domain_trace(case, "engine"))
    return normalise_failures(
        [
            {
                "rule_id": f.rule_id,
                "kind": f.rule_kind,
                "span_id": f.offending_span_id,
                "offending_action": f.offending_action,
                "severity": f.severity.value,
            }
            for f in result.failures
        ]
    )


def normalise_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Sorted, because neither side promises an order and asserting on one would make this suite fail
    # for a reason that is not a divergence.
    return sorted(
        (
            {
                "rule_id": f["rule_id"],
                "kind": f["kind"],
                "span_id": f.get("span_id"),
                "offending_action": f.get("offending_action"),
                "severity": f["severity"],
            }
            for f in failures
        ),
        key=lambda f: (f["rule_id"], str(f["span_id"])),
    )


@pytest.mark.parametrize("path", trajectory_cases(), ids=lambda p: p.stem)
async def test_a_trace_through_the_database_reaches_the_same_failures(
    path: Path, client: AsyncClient, tenant_a: Tenant
) -> None:
    """The same policy and the same trace, judged in memory and after a round trip.

    This is the trajectory half of "same verdict everywhere", and it is the half with a real
    transformation in the middle: the server writes spans to columns and JSONB, then rebuilds a
    domain trace from them. Ordering, aliases, and tool arguments all have to survive that, and each
    one is silent when it does not — a policy that quietly stops firing looks exactly like a policy
    with nothing to report.
    """
    case = load(path)
    trace_id = f"parity-{uuid.uuid4().hex[:8]}"
    head = auth(tenant_a)

    await client.post(
        "/v1/ingest/traces",
        headers=head,
        json={
            "resource": {"environment": "production"},
            "traces": [
                {
                    "trace_id": trace_id,
                    "name": "parity",
                    "started_at": BASE.isoformat(),
                    # Ended, because online evaluation only considers finished traces: a policy
                    # about what did not happen cannot be checked while it still might.
                    "ended_at": (BASE + timedelta(seconds=1)).isoformat(),
                    "state": case.get("state") or {},
                    "metadata": case.get("metadata") or {},
                }
            ],
            "spans": [
                {
                    "trace_id": trace_id,
                    "span_id": row["span_id"],
                    "name": row["name"],
                    "span_type": row["span_type"],
                    "tool_name": row.get("tool_name"),
                    "tool_args": row.get("tool_args"),
                    "started_at": (BASE + timedelta(milliseconds=row["offset_ms"])).isoformat(),
                    "ended_at": (BASE + timedelta(milliseconds=row["offset_ms"] + 5)).isoformat(),
                    "sequence_index": index,
                }
                for index, row in enumerate(case["spans"])
            ],
        },
    )

    policy = (
        await client.post(
            "/v1/trajectory-policies",
            headers=head,
            json={"name": path.stem, "slug": f"parity-{path.stem}"},
        )
    ).json()
    version = (
        await client.post(
            f"/v1/trajectory-policies/{policy['id']}/versions",
            headers=head,
            json={"source_yaml": case["policy_yaml"]},
        )
    ).json()
    await client.post(
        "/v1/online-rules",
        headers=head,
        json={
            "name": path.stem,
            "slug": f"parity-{path.stem}"[:100],
            "kind": "trajectory",
            "policy_version_id": version["id"],
            # Every trace, and no escalation: this test is about the verdict, not the queue.
            "sample_rate": 1.0,
            "escalate_on_failure": False,
            "trace_name": "parity",
        },
    )

    ran = (await client.post("/v1/online-rules/run", headers=head)).json()
    assert ran["evaluations_written"] >= 1, f"the rule did not evaluate the trace: {ran}"

    evaluation = await _evaluation_for(client, head, trace_id)
    expected = engine_failures(case)
    actual = normalise_failures((evaluation.get("detail") or {}).get("failures") or [])
    assert actual == expected, (
        f"{path.name}: the engine and the server disagree about this trace.\n"
        f"why this case exists: {case['why']}\n"
        f"engine: {json.dumps(expected, indent=2)}\n"
        f"server: {json.dumps(actual, indent=2)}"
    )


async def _evaluation_for(
    client: AsyncClient, head: dict[str, str], trace_id: str
) -> dict[str, Any]:
    """The stored online evaluation for one trace.

    Read back through the trace API rather than the ORM, so the assertion is about what a caller can
    actually see — an evaluation the server computed correctly and then failed to expose is still a
    product that does not work.
    """
    detail = (await client.get(f"/v1/traces/{trace_id}", headers=head)).json()
    evaluations = detail.get("evaluations") or []
    assert evaluations, f"no online evaluation was exposed for {trace_id}"
    return dict(evaluations[0])


def test_there_are_trajectory_fixtures() -> None:
    assert len(trajectory_cases()) >= 4, (
        f"expected trajectory parity fixtures in {TRAJECTORY}, found {len(trajectory_cases())}"
    )
