"""The endpoints a publishing CLI needs, and the boundary they enforce.

Two capabilities, both added because publishing exposed that a run could be recorded and still be
useless:

- **Submitting metrics the server cannot compute.** A corpus metric — a confusion matrix, per-class
  recall, p95 latency — is a property of the whole run, not a sum over per-example scores, so only
  the process that ran the suite has it. Without a way to send them, every gate on one evaluates as
  "metric missing" and the server reads ERROR on a run the CLI passed. That is not hypothetical: it
  is what happened the first time a suite with a protected-class gate was published.

- **Resolving the baseline before the run.** Otherwise the local evaluation skips regression rules
  the server later applies, and the two verdicts differ for a reason that is legitimate and
  indistinguishable from a bug.

The boundary that matters is in the first one: submitted metrics are stored **only** for keys the
server did not compute itself. Anything derived from per-example scores stays the server's own
number, because that recomputation is what makes its verdict verified rather than merely reported.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from factories import Tenant
from httpx import ASGITransport, AsyncClient
from proofstep_api.api.dependencies import get_session
from proofstep_api.main import create_app
from proofstep_api.settings import Settings
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


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


async def make_run(
    client: AsyncClient,
    tenant: Tenant,
    *,
    suite: str = "publishing",
    branch: str = "main",
    scores: float = 1.0,
) -> str:
    """A completed run with one per-example metric, through the public API only."""
    head = auth(tenant)
    slug = f"{suite}-{uuid.uuid4().hex[:6]}"

    dataset = (
        await client.post(
            "/v1/datasets", headers=head, json={"name": slug, "slug": slug, "kind": "golden"}
        )
    ).json()
    version = (
        await client.post(
            f"/v1/datasets/{dataset['id']}/versions", headers=head, json={"version": "v1"}
        )
    ).json()
    await client.post(
        f"/v1/dataset-versions/{version['id']}/examples",
        headers=head,
        json={"examples": [{"id": "e1", "input": {"a": 1}}]},
    )
    await client.post(f"/v1/dataset-versions/{version['id']}/lock", headers=head, json={})

    experiment = (
        await client.post(
            "/v1/experiments",
            headers=head,
            json={
                "name": slug,
                "suite_name": suite,
                "dataset_version_id": version["id"],
                "git_branch": branch,
                "git_commit": "a" * 40,
            },
        )
    ).json()
    run = (
        await client.post(f"/v1/experiments/{experiment['id']}/runs", headers=head, json={})
    ).json()
    await client.post(
        f"/v1/experiment-runs/{run['id']}/results",
        headers=head,
        json={
            "results": [{"example_id": "e1", "scores": [{"metric": "accuracy", "value": scores}]}]
        },
    )
    await client.post(
        f"/v1/experiment-runs/{run['id']}/complete", headers=head, json={"status": "succeeded"}
    )
    return str(run["id"])


async def metrics_for(client: AsyncClient, tenant: Tenant, run_id: str) -> dict[str, Any]:
    rows = (await client.get(f"/v1/experiment-runs/{run_id}/metrics", headers=auth(tenant))).json()
    return {row["key"]: row for row in rows}


class TestSubmittedMetrics:
    async def test_a_corpus_metric_is_stored(self, client: AsyncClient, tenant_a: Tenant) -> None:
        run_id = await make_run(client, tenant_a)
        response = await client.post(
            f"/v1/experiment-runs/{run_id}/metrics",
            headers=auth(tenant_a),
            json={
                "metrics": [
                    {
                        "key": "classes_recall",
                        "value": 0.4,
                        "count": 3,
                        "slice": {"class": "unsubscribe"},
                    },
                    {"key": "p95_latency_ms", "value": 812.0, "count": 40, "unit": "ms"},
                ]
            },
        )
        assert response.status_code == 200
        assert response.json()["stored"] == 2

        stored = await metrics_for(client, tenant_a, run_id)
        assert stored["p95_latency_ms"]["value"] == 812.0
        assert stored["classes_recall"]["slice"] == {"class": "unsubscribe"}

    async def test_a_computed_metric_cannot_be_overwritten(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """The boundary, and the reason the whole feature is safe.

        `accuracy` was recomputed server-side from the stored scores. If a client could replace it,
        a run could claim any number it liked and the dashboard would agree — which would hollow out
        the guarantee that the server's verdict is verified rather than reported.
        """
        run_id = await make_run(client, tenant_a, scores=1.0)
        response = await client.post(
            f"/v1/experiment-runs/{run_id}/metrics",
            headers=auth(tenant_a),
            json={"metrics": [{"key": "accuracy", "value": 0.0, "count": 1}]},
        )
        assert response.json() == {"stored": 0, "rejected": ["accuracy"]}

        stored = await metrics_for(client, tenant_a, run_id)
        assert stored["accuracy"]["value"] == 1.0, "the client's number replaced the server's"

    async def test_a_duplicate_inside_one_submission_is_refused(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        # Two rows with the same key are indistinguishable to every reader downstream, so the second
        # is rejected rather than stored beside the first.
        run_id = await make_run(client, tenant_a)
        response = await client.post(
            f"/v1/experiment-runs/{run_id}/metrics",
            headers=auth(tenant_a),
            json={
                "metrics": [
                    {"key": "ndcg", "value": 0.8, "count": 1},
                    {"key": "ndcg", "value": 0.2, "count": 1},
                ]
            },
        )
        assert response.json() == {"stored": 1, "rejected": ["ndcg"]}

    async def test_the_same_key_in_different_slices_is_not_a_duplicate(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        # Per-class metrics are the normal case: one key, many slices. Treating them as duplicates
        # would drop every class but the first, which is exactly the data a protected gate reads.
        run_id = await make_run(client, tenant_a)
        response = await client.post(
            f"/v1/experiment-runs/{run_id}/metrics",
            headers=auth(tenant_a),
            json={
                "metrics": [
                    {"key": "recall", "value": 0.9, "count": 2, "slice": {"class": "meeting"}},
                    {"key": "recall", "value": 0.1, "count": 2, "slice": {"class": "unsubscribe"}},
                ]
            },
        )
        assert response.json()["stored"] == 2

    async def test_another_tenants_run_is_not_found(
        self, client: AsyncClient, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        run_id = await make_run(client, tenant_a)
        response = await client.post(
            f"/v1/experiment-runs/{run_id}/metrics",
            headers=auth(tenant_b),
            json={"metrics": [{"key": "ndcg", "value": 0.8, "count": 1}]},
        )
        assert response.status_code == 404


class TestBaselineResolution:
    async def test_no_baseline_is_an_answer_not_an_error(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """The first run of a new suite is the normal case.

        A 404 here would make "nothing to compare against yet" look like a failed lookup, and the
        client would have to special-case a status code to tell them apart.
        """
        response = await client.get(
            "/v1/experiments/baseline",
            headers=auth(tenant_a),
            params={"suite_name": "never-run", "branch": "main"},
        )
        assert response.status_code == 200
        assert response.json()["run_id"] is None
        assert response.json()["metrics"] == []

    async def test_it_returns_the_latest_run_on_the_branch_with_its_metrics(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        first = await make_run(client, tenant_a, suite="baseline-demo", scores=1.0)
        await client.post(
            f"/v1/experiment-runs/{first}/metrics",
            headers=auth(tenant_a),
            json={"metrics": [{"key": "p95_latency_ms", "value": 500.0, "count": 1}]},
        )

        response = await client.get(
            "/v1/experiments/baseline",
            headers=auth(tenant_a),
            params={"suite_name": "baseline-demo", "branch": "main"},
        )
        body = response.json()
        assert body["run_id"] == first
        # Both kinds come back: the metrics the server computed and the ones it was given. A
        # baseline missing its corpus metrics would silently skip every regression rule on them.
        keys = {metric["key"] for metric in body["metrics"]}
        assert {"accuracy", "p95_latency_ms"} <= keys

    async def test_a_run_on_another_branch_is_not_the_baseline(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        # "Did my branch make it worse than main?" is the question. A feature branch's own run
        # answering it would compare a change against itself.
        await make_run(client, tenant_a, suite="branchy", branch="feature/x")
        response = await client.get(
            "/v1/experiments/baseline",
            headers=auth(tenant_a),
            params={"suite_name": "branchy", "branch": "main"},
        )
        assert response.json()["run_id"] is None

    async def test_it_does_not_reach_across_tenants(
        self, client: AsyncClient, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        await make_run(client, tenant_a, suite="shared-name")
        response = await client.get(
            "/v1/experiments/baseline",
            headers=auth(tenant_b),
            params={"suite_name": "shared-name", "branch": "main"},
        )
        assert response.json()["run_id"] is None


class TestSubmittingWhatTheServerAlreadyHas:
    """Resubmitting a computed metric must cost that metric, not the whole submission.

    This is the last request of every publish, and it carries the corpus metrics — a protected
    class's recall, p95 latency — that cannot be reconstructed from per-example scores by anyone
    but the process that ran the suite. They all travel in one INSERT.

    The original code decided what to insert by first reading which keys the server had computed,
    then inserting the rest. Correct in a quiet room, and one race from wrong: a key appearing
    between the SELECT and the INSERT made the statement fail on the unique index, taking every
    other metric in the submission with it. The run then had no `classes_recall`, so its protected
    gate read ERROR — "metric missing" — on a run the CLI had already called a failure. A verdict
    disagreeing between CLI and dashboard is the one outcome this system exists to prevent, and it
    arrived as a 500 in the last request of a publish.

    Seen for real in CI, where the API log showed all 41 of a run's metrics rolled back over one
    duplicate key, leaving the run without the corpus metrics its gate needed.

    The tests below pin the behaviour that must hold; they do **not** reproduce that failure, and
    neither did anything else tried. Sequentially the old code was correct, and two submissions
    raced through asyncio commit in whatever order the loop picks, which was reliably the safe one.
    So this is a fix by construction rather than by reproduction: `ON CONFLICT DO NOTHING` has no
    window between deciding and writing, which is the only place the old version could go wrong.
    Stated plainly because a fix whose trigger was never reproduced deserves to be labelled as one.
    """

    async def test_the_run_keeps_the_metrics_it_could_not_compute(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        run_id = await make_run(client, tenant_a)
        computed = (
            await client.get(f"/v1/experiment-runs/{run_id}/metrics", headers=auth(tenant_a))
        ).json()
        already = {metric["key"] for metric in computed}
        assert already, "the run must have server-computed metrics for this test to mean anything"

        # A realistic publish: every computed key resubmitted, plus the corpus metric only the
        # client has. Exactly the shape that produced the 500.
        submission = [{"key": key, "value": 0.123, "count": 1} for key in sorted(already)] + [
            {"key": "classes_recall", "value": 0.4, "count": 5, "slice": {"class": "unsubscribe"}}
        ]

        response = await client.post(
            f"/v1/experiment-runs/{run_id}/metrics",
            headers=auth(tenant_a),
            json={"metrics": submission},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stored"] == 1, body
        assert set(body["rejected"]) == already, body

        # The corpus metric survived, and the server's own numbers were not overwritten by the
        # client's 0.123 — which is what makes the server's verdict verified rather than reported.
        stored = {
            metric["key"]: metric["value"]
            for metric in (
                await client.get(f"/v1/experiment-runs/{run_id}/metrics", headers=auth(tenant_a))
            ).json()
        }
        assert "classes_recall" in stored
        for key in already:
            assert stored[key] != 0.123, f"{key} was overwritten by the client's value"

    async def test_submitting_the_same_payload_twice_is_stable(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """A retried publish must not destroy the first one's metrics.

        The CLI does not retry today, but a proxy, a timeout, or an operator running the command
        again all produce the same request — and the honest answer to "I already have that" is to
        say so, not to fail the request.
        """
        run_id = await make_run(client, tenant_a)
        payload = {"metrics": [{"key": "ndcg", "value": 0.8, "count": 1}]}

        first = await client.post(
            f"/v1/experiment-runs/{run_id}/metrics", headers=auth(tenant_a), json=payload
        )
        second = await client.post(
            f"/v1/experiment-runs/{run_id}/metrics", headers=auth(tenant_a), json=payload
        )

        assert first.json() == {"stored": 1, "rejected": []}
        assert second.status_code == 200, second.text
        assert second.json() == {"stored": 0, "rejected": ["ndcg"]}
