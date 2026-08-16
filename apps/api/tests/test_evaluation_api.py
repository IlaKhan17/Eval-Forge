"""Dataset locking, evaluator versioning, experiments, comparison, and gate parity.

The last class is the important one. If the server and the CLI can disagree about a
verdict, the product's core promise fails: the CI exit code and the dashboard must
say the same thing about the same run.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from factories import Tenant, make_tenant
from httpx import ASGITransport, AsyncClient
from proofstep_api.api.dependencies import get_session
from proofstep_api.db.models.evaluation import DatasetExample
from proofstep_api.main import create_app
from proofstep_api.services.experiments import ExperimentService
from proofstep_api.settings import Settings
from sqlalchemy.ext.asyncio import AsyncSession

from proofstep_core.gates import evaluate_gates
from proofstep_types import GateRule, GateSet, Severity, Verdict

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    settings = Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32")
    app = create_app(settings)

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


def auth(tenant: Tenant) -> dict[str, str]:
    return {"Authorization": f"Bearer {tenant.token}"}


def result(
    example_id: str, metric: str, value: float | None = None, **score: Any
) -> dict[str, Any]:
    """One example result carrying a single score."""
    entry: dict[str, Any] = {"metric": metric, **score}
    if value is not None:
        entry["value"] = value
    return {"example_id": example_id, "status": "ok", "scores": [entry]}


async def make_locked_dataset(
    client: AsyncClient, tenant: Tenant, *, slug: str = "email-quality", n: int = 3
) -> dict[str, Any]:
    dataset = (
        await client.post(
            "/v1/datasets", json={"name": "Email quality", "slug": slug}, headers=auth(tenant)
        )
    ).json()
    version = (
        await client.post(
            f"/v1/datasets/{dataset['id']}/versions", json={"version": "v1"}, headers=auth(tenant)
        )
    ).json()
    await client.post(
        f"/v1/dataset-versions/{version['id']}/examples",
        json={
            "examples": [
                {"id": f"ex-{i}", "input": {"q": i}, "expected": {"a": i * 2}} for i in range(n)
            ]
        },
        headers=auth(tenant),
    )
    locked = await client.post(f"/v1/dataset-versions/{version['id']}/lock", headers=auth(tenant))
    return dict(locked.json())


class TestDatasetLocking:
    async def test_lock_records_a_content_hash(self, client: AsyncClient, tenant_a: Tenant) -> None:
        locked = await make_locked_dataset(client, tenant_a)
        assert locked["status"] == "locked"
        assert locked["content_hash"] is not None
        assert len(locked["content_hash"]) == 64
        assert locked["example_count"] == 3

    async def test_the_hash_is_content_derived_not_random(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        """Two versions with identical content must hash identically.

        That is what makes the hash a proof of sameness rather than a random tag.
        """
        a = await make_locked_dataset(client, tenant_a, slug="same")
        b = await make_locked_dataset(client, tenant_b, slug="same")
        assert a["content_hash"] == b["content_hash"]

    async def test_different_content_hashes_differently(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        a = await make_locked_dataset(client, tenant_a, slug="three", n=3)
        b = await make_locked_dataset(client, tenant_a, slug="four", n=4)
        assert a["content_hash"] != b["content_hash"]

    async def test_writing_to_a_locked_version_is_409(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        locked = await make_locked_dataset(client, tenant_a)
        response = await client.post(
            f"/v1/dataset-versions/{locked['id']}/examples",
            json={"examples": [{"id": "late", "input": {}}]},
            headers=auth(tenant_a),
        )
        assert response.status_code == 409
        assert "locked" in response.json()["detail"]

    async def test_the_database_refuses_too_even_bypassing_the_service(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Defence in depth: the trigger holds when the application layer is bypassed.

        Reproducibility enforced in exactly one place is one bug away from being
        false, so the check exists in the service *and* in the database.
        """
        from sqlalchemy.exc import InternalError, ProgrammingError

        locked = await make_locked_dataset(client, tenant_a)
        session.add(
            DatasetExample(
                project_id=tenant_a.project.id,
                dataset_version_id=uuid.UUID(locked["id"]),
                ordinal=99,
                external_id="smuggled",
                input={},
            )
        )
        with pytest.raises((InternalError, ProgrammingError), match="locked"):
            await session.flush()
        await session.rollback()

    async def test_locking_is_idempotent(self, client: AsyncClient, tenant_a: Tenant) -> None:
        """A retried CI step must not fail on a step that already succeeded."""
        locked = await make_locked_dataset(client, tenant_a)
        again = await client.post(
            f"/v1/dataset-versions/{locked['id']}/lock", headers=auth(tenant_a)
        )
        assert again.status_code == 200
        assert again.json()["content_hash"] == locked["content_hash"]

    async def test_locking_an_empty_version_is_refused(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """An empty dataset produces a passing experiment, which looks like success."""
        dataset = (
            await client.post(
                "/v1/datasets", json={"name": "Empty", "slug": "empty"}, headers=auth(tenant_a)
            )
        ).json()
        version = (
            await client.post(
                f"/v1/datasets/{dataset['id']}/versions",
                json={"version": "v1"},
                headers=auth(tenant_a),
            )
        ).json()
        response = await client.post(
            f"/v1/dataset-versions/{version['id']}/lock", headers=auth(tenant_a)
        )
        assert response.status_code == 422
        assert "measured nothing" in response.json()["detail"]

    async def test_duplicate_example_ids_are_refused(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        dataset = (
            await client.post(
                "/v1/datasets", json={"name": "Dup", "slug": "dup"}, headers=auth(tenant_a)
            )
        ).json()
        version = (
            await client.post(
                f"/v1/datasets/{dataset['id']}/versions",
                json={"version": "v1"},
                headers=auth(tenant_a),
            )
        ).json()
        response = await client.post(
            f"/v1/dataset-versions/{version['id']}/examples",
            json={"examples": [{"id": "x", "input": {}}, {"id": "x", "input": {}}]},
            headers=auth(tenant_a),
        )
        assert response.status_code == 409

    async def test_resolve_by_slug_and_label(self, client: AsyncClient, tenant_a: Tenant) -> None:
        """Suite files reference datasets by name; a UUID in git is unmergeable."""
        await make_locked_dataset(client, tenant_a, slug="named")
        found = await client.get(
            "/v1/dataset-versions/resolve?dataset=named&version=latest-locked",
            headers=auth(tenant_a),
        )
        assert found.status_code == 200
        assert found.json()["status"] == "locked"

    async def test_cross_tenant_version_access_is_404(
        self, client: AsyncClient, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        locked = await make_locked_dataset(client, tenant_a, slug="mine")
        response = await client.get(
            f"/v1/dataset-versions/{locked['id']}/examples", headers=auth(tenant_b)
        )
        assert response.status_code == 404


class TestEvaluatorVersioning:
    async def test_an_identical_config_reuses_the_version(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """Otherwise every CI run mints a 'new' version and comparison refuses
        every pair as evaluator drift."""
        created = (
            await client.post(
                "/v1/evaluators",
                json={"name": "Groundedness", "slug": "grounded", "evaluator_type": "llm_judge"},
                headers=auth(tenant_a),
            )
        ).json()
        body = {"config": {"rubric": "be strict"}, "judge_model": "pinned-model-v1"}

        first = (
            await client.post(
                f"/v1/evaluators/{created['id']}/versions", json=body, headers=auth(tenant_a)
            )
        ).json()
        second = (
            await client.post(
                f"/v1/evaluators/{created['id']}/versions", json=body, headers=auth(tenant_a)
            )
        ).json()

        assert first["version"] == second["version"] == 1
        assert second["reused"] is True

    async def test_changing_the_judge_model_mints_a_new_version(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """A judge whose model silently upgrades invalidates every historical number,
        so the pin is part of the version's identity."""
        created = (
            await client.post(
                "/v1/evaluators",
                json={"name": "G", "slug": "g", "evaluator_type": "llm_judge"},
                headers=auth(tenant_a),
            )
        ).json()
        first = (
            await client.post(
                f"/v1/evaluators/{created['id']}/versions",
                json={"config": {"rubric": "same"}, "judge_model": "model-a"},
                headers=auth(tenant_a),
            )
        ).json()
        second = (
            await client.post(
                f"/v1/evaluators/{created['id']}/versions",
                json={"config": {"rubric": "same"}, "judge_model": "model-b"},
                headers=auth(tenant_a),
            )
        ).json()

        assert second["version"] == 2
        assert first["config_hash"] != second["config_hash"]

    async def test_changing_the_rubric_mints_a_new_version(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """Editing a rubric silently redefines the metric — a changed ruler."""
        created = (
            await client.post(
                "/v1/evaluators",
                json={"name": "G", "slug": "g2", "evaluator_type": "llm_judge"},
                headers=auth(tenant_a),
            )
        ).json()
        await client.post(
            f"/v1/evaluators/{created['id']}/versions",
            json={"config": {"rubric": "v1"}},
            headers=auth(tenant_a),
        )
        second = (
            await client.post(
                f"/v1/evaluators/{created['id']}/versions",
                json={"config": {"rubric": "v2"}},
                headers=auth(tenant_a),
            )
        ).json()
        assert second["version"] == 2


class TestExperimentLifecycle:
    async def test_an_experiment_requires_a_locked_dataset(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """A draft can change underneath the run, making the result unreproducible."""
        dataset = (
            await client.post(
                "/v1/datasets", json={"name": "D", "slug": "draft-ds"}, headers=auth(tenant_a)
            )
        ).json()
        version = (
            await client.post(
                f"/v1/datasets/{dataset['id']}/versions",
                json={"version": "v1"},
                headers=auth(tenant_a),
            )
        ).json()

        response = await client.post(
            "/v1/experiments",
            json={"name": "run", "suite_name": "s", "dataset_version_id": version["id"]},
            headers=auth(tenant_a),
        )
        assert response.status_code == 422
        assert "cannot be reproduced" in response.json()["detail"]

    async def test_the_experiment_records_the_dataset_hash(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        locked = await make_locked_dataset(client, tenant_a)
        experiment = (
            await client.post(
                "/v1/experiments",
                json={
                    "name": "run",
                    "suite_name": "sdr-email",
                    "dataset_version_id": locked["id"],
                    "git_commit": "abc123",
                    "git_branch": "main",
                },
                headers=auth(tenant_a),
            )
        ).json()
        assert experiment["dataset_content_hash"] == locked["content_hash"]

    async def test_a_full_run_aggregates_metrics(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        locked = await make_locked_dataset(client, tenant_a)
        experiment = (
            await client.post(
                "/v1/experiments",
                json={"name": "r", "suite_name": "s", "dataset_version_id": locked["id"]},
                headers=auth(tenant_a),
            )
        ).json()
        run = (
            await client.post(f"/v1/experiments/{experiment['id']}/runs", headers=auth(tenant_a))
        ).json()

        await client.post(
            f"/v1/experiment-runs/{run['id']}/results",
            json={
                "results": [
                    {
                        "example_id": "ex-0",
                        "status": "ok",
                        "scores": [{"metric": "acc", "value": 1.0}],
                    },
                    {
                        "example_id": "ex-1",
                        "status": "ok",
                        "scores": [{"metric": "acc", "value": 0.0}],
                    },
                ]
            },
            headers=auth(tenant_a),
        )
        await client.post(
            f"/v1/experiment-runs/{run['id']}/complete",
            json={"status": "succeeded"},
            headers=auth(tenant_a),
        )

        metrics = (
            await client.get(f"/v1/experiment-runs/{run['id']}/metrics", headers=auth(tenant_a))
        ).json()
        acc = next(m for m in metrics if m["key"] == "acc")
        assert acc["value"] == pytest.approx(0.5)
        assert acc["count"] == 2

    async def test_resending_a_chunk_of_results_is_safe(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """The CLI streams in chunks; a half-succeeded chunk must be re-sendable."""
        locked = await make_locked_dataset(client, tenant_a)
        experiment = (
            await client.post(
                "/v1/experiments",
                json={"name": "r", "suite_name": "s", "dataset_version_id": locked["id"]},
                headers=auth(tenant_a),
            )
        ).json()
        run = (
            await client.post(f"/v1/experiments/{experiment['id']}/runs", headers=auth(tenant_a))
        ).json()
        payload = {"results": [{"example_id": "ex-0", "status": "ok", "scores": []}]}

        first = (
            await client.post(
                f"/v1/experiment-runs/{run['id']}/results", json=payload, headers=auth(tenant_a)
            )
        ).json()
        second = (
            await client.post(
                f"/v1/experiment-runs/{run['id']}/results", json=payload, headers=auth(tenant_a)
            )
        ).json()

        assert first == {"stored": 1, "skipped": 0}
        assert second == {"stored": 0, "skipped": 1}

    async def test_an_errored_score_is_not_stored_as_a_zero(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The invariant that survives the whole round trip through the database."""
        locked = await make_locked_dataset(client, tenant_a)
        experiment = (
            await client.post(
                "/v1/experiments",
                json={"name": "r", "suite_name": "s", "dataset_version_id": locked["id"]},
                headers=auth(tenant_a),
            )
        ).json()
        run = (
            await client.post(f"/v1/experiments/{experiment['id']}/runs", headers=auth(tenant_a))
        ).json()

        await client.post(
            f"/v1/experiment-runs/{run['id']}/results",
            json={
                "results": [
                    {
                        "example_id": "ex-0",
                        "status": "ok",
                        "scores": [{"metric": "j", "value": 1.0}],
                    },
                    {
                        "example_id": "ex-1",
                        "status": "ok",
                        "scores": [{"metric": "j", "error": "provider timeout"}],
                    },
                ]
            },
            headers=auth(tenant_a),
        )
        await client.post(
            f"/v1/experiment-runs/{run['id']}/complete", json={}, headers=auth(tenant_a)
        )

        metrics = (
            await client.get(f"/v1/experiment-runs/{run['id']}/metrics", headers=auth(tenant_a))
        ).json()
        judge = next(m for m in metrics if m["key"] == "j")
        # One measurement of 1.0 plus one failure to measure is 1.0, not 0.5.
        assert judge["value"] == 1.0
        assert judge["count"] == 1
        assert judge["error_count"] == 1


class TestComparison:
    async def _run_with(
        self,
        client: AsyncClient,
        tenant: Tenant,
        *,
        value: float,
        branch: str,
        suite: str = "s",
        examples: int = 3,
    ) -> dict[str, Any]:
        slug = f"ds-{uuid.uuid4().hex[:8]}"
        locked = await make_locked_dataset(client, tenant, slug=slug, n=examples)
        experiment = (
            await client.post(
                "/v1/experiments",
                json={
                    "name": "r",
                    "suite_name": suite,
                    "dataset_version_id": locked["id"],
                    "git_branch": branch,
                },
                headers=auth(tenant),
            )
        ).json()
        run = (
            await client.post(f"/v1/experiments/{experiment['id']}/runs", headers=auth(tenant))
        ).json()
        await client.post(
            f"/v1/experiment-runs/{run['id']}/results",
            json={
                "results": [
                    {
                        "example_id": f"ex-{i}",
                        "status": "ok",
                        "scores": [{"metric": "acc", "value": value}],
                    }
                    for i in range(examples)
                ]
            },
            headers=auth(tenant),
        )
        await client.post(
            f"/v1/experiment-runs/{run['id']}/complete", json={}, headers=auth(tenant)
        )
        return dict(run)

    async def test_deltas_are_reported(self, client: AsyncClient, tenant_a: Tenant) -> None:
        baseline = await self._run_with(client, tenant_a, value=1.0, branch="main")
        candidate = await self._run_with(client, tenant_a, value=0.5, branch="feature")

        body = (
            await client.post(
                "/v1/experiments/compare",
                json={"candidate_run_id": candidate["id"], "baseline_run_id": baseline["id"]},
                headers=auth(tenant_a),
            )
        ).json()
        delta = next(m for m in body["metrics"] if m["key"] == "acc")
        assert delta["absolute_delta"] == pytest.approx(-0.5)

    async def test_identical_content_is_not_flagged_as_a_mismatch(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """Two *different* dataset rows with the same content are the same data.

        Content addressing means the hash tracks content, not identity — which is
        the property that makes it a proof of sameness rather than a random tag.
        """
        baseline = await self._run_with(client, tenant_a, value=1.0, branch="main", examples=3)
        candidate = await self._run_with(client, tenant_a, value=0.5, branch="feature", examples=3)
        body = (
            await client.post(
                "/v1/experiments/compare",
                json={"candidate_run_id": candidate["id"], "baseline_run_id": baseline["id"]},
                headers=auth(tenant_a),
            )
        ).json()
        assert body["dataset_match"] is True
        assert not any("Dataset content differs" in w for w in body["warnings"])

    async def test_differing_content_is_flagged(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """Comparing across different data yields a confidently wrong conclusion,
        so it is reported rather than quietly rendered."""
        baseline = await self._run_with(client, tenant_a, value=1.0, branch="main", examples=3)
        candidate = await self._run_with(client, tenant_a, value=0.5, branch="feature", examples=5)
        body = (
            await client.post(
                "/v1/experiments/compare",
                json={"candidate_run_id": candidate["id"], "baseline_run_id": baseline["id"]},
                headers=auth(tenant_a),
            )
        ).json()
        assert body["dataset_match"] is False
        assert any("Dataset content differs" in w for w in body["warnings"])

    async def test_the_baseline_is_resolved_from_the_branch_when_omitted(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """So a CI job can say 'compare against main' without a lookup first."""
        await self._run_with(client, tenant_a, value=1.0, branch="main", suite="auto")
        candidate = await self._run_with(client, tenant_a, value=0.9, branch="pr", suite="auto")

        body = (
            await client.post(
                "/v1/experiments/compare",
                json={"candidate_run_id": candidate["id"], "baseline_branch": "main"},
                headers=auth(tenant_a),
            )
        ).json()
        assert body["baseline_run_id"] is not None

    async def test_a_promoted_baseline_takes_precedence(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        run = await self._run_with(client, tenant_a, value=1.0, branch="other", suite="promoted")
        service = ExperimentService(session, project_id=tenant_a.project.id)
        experiment = await service.get_run(uuid.UUID(run["id"]))
        await client.post(
            f"/v1/experiments/{experiment.experiment_id}/promote-baseline", headers=auth(tenant_a)
        )

        candidate = await self._run_with(client, tenant_a, value=0.5, branch="pr", suite="promoted")
        body = (
            await client.post(
                "/v1/experiments/compare",
                json={"candidate_run_id": candidate["id"]},
                headers=auth(tenant_a),
            )
        ).json()
        assert body["baseline_run_id"] == run["id"]


class TestGateParity:
    """The server must reach the same verdict as the CLI, on the same inputs.

    Both sides call the identical `evaluation-core` functions, and this asserts it
    holds end to end through the database — serialization, aggregation, and gate
    evaluation included. The day these disagree, neither the CI exit code nor the
    dashboard is trustworthy.
    """

    async def _seeded_run(
        self, client: AsyncClient, tenant: Tenant, results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        locked = await make_locked_dataset(
            client, tenant, slug=f"p-{uuid.uuid4().hex[:8]}", n=len(results)
        )
        experiment = (
            await client.post(
                "/v1/experiments",
                json={"name": "r", "suite_name": "parity", "dataset_version_id": locked["id"]},
                headers=auth(tenant),
            )
        ).json()
        run = (
            await client.post(f"/v1/experiments/{experiment['id']}/runs", headers=auth(tenant))
        ).json()
        await client.post(
            f"/v1/experiment-runs/{run['id']}/results",
            json={"results": results},
            headers=auth(tenant),
        )
        await client.post(
            f"/v1/experiment-runs/{run['id']}/complete", json={}, headers=auth(tenant)
        )
        return dict(run)

    async def test_the_hidden_regression_scenario_agrees_end_to_end(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The rare-class collapse, run through the database and the API.

        An aggregate gate passes; the sliced protected floor fails. Both the local
        engine and the server must say so, with the same blocking metric.
        """
        # 20 examples: one rare-class member, which the candidate gets wrong.
        results: list[dict[str, Any]] = []
        for i in range(20):
            rare = i == 0
            correct = not rare
            results.append(
                {
                    "example_id": f"ex-{i}",
                    "status": "ok",
                    "scores": [
                        {"metric": "accuracy", "value": 1.0 if correct else 0.0},
                        {
                            "metric": "per_class_recall",
                            "value": 1.0 if correct else 0.0,
                            "slice": {"class": "unsubscribe" if rare else "other"},
                        },
                    ],
                }
            )

        run = await self._seeded_run(client, tenant_a, results)

        gate_set = (
            await client.post(
                "/v1/quality-gate-sets",
                json={
                    "name": "protected",
                    "require_dataset_match": False,
                    "rules": [
                        {"metric_key": "accuracy", "minimum": 0.90},
                        {
                            "metric_key": "per_class_recall",
                            "minimum": 0.98,
                            "slice": {"class": "unsubscribe"},
                        },
                    ],
                },
                headers=auth(tenant_a),
            )
        ).json()

        server = (
            await client.post(
                "/v1/experiments/compare",
                json={"candidate_run_id": run["id"], "gate_set_id": gate_set["id"]},
                headers=auth(tenant_a),
            )
        ).json()

        # Now the same decision computed locally, from the same stored metrics.
        service = ExperimentService(session, project_id=tenant_a.project.id)
        metrics = await service.load_metrics(uuid.UUID(run["id"]))
        local = evaluate_gates(
            GateSet(
                name="protected",
                require_dataset_match=False,
                rules=[
                    GateRule(metric_key="accuracy", minimum=0.90),
                    GateRule(
                        metric_key="per_class_recall",
                        minimum=0.98,
                        slice={"class": "unsubscribe"},
                        severity=Severity.BLOCK,
                    ),
                ],
            ),
            metrics,
            None,
        )

        assert server["verdict"] == local.verdict.value == Verdict.FAIL.value
        assert server["exit_code"] == local.exit_code == 1

        server_blocking = sorted(g["metric_key"] for g in server["gates"] if g["verdict"] == "fail")
        local_blocking = sorted(f.metric_key for f in local.blocking_failures)
        assert server_blocking == local_blocking == ["per_class_recall"]

        # And the premise: the aggregate on its own would have passed.
        aggregate_only = next(g for g in server["gates"] if g["metric_key"] == "accuracy")
        assert aggregate_only["verdict"] == "pass"

    async def test_a_passing_run_agrees_too(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        results = [
            {
                "example_id": f"ex-{i}",
                "status": "ok",
                "scores": [{"metric": "accuracy", "value": 1.0}],
            }
            for i in range(5)
        ]
        run = await self._seeded_run(client, tenant_a, results)
        gate_set = (
            await client.post(
                "/v1/quality-gate-sets",
                json={
                    "name": "simple",
                    "require_dataset_match": False,
                    "rules": [{"metric_key": "accuracy", "minimum": 0.9}],
                },
                headers=auth(tenant_a),
            )
        ).json()

        server = (
            await client.post(
                "/v1/experiments/compare",
                json={"candidate_run_id": run["id"], "gate_set_id": gate_set["id"]},
                headers=auth(tenant_a),
            )
        ).json()

        service = ExperimentService(session, project_id=tenant_a.project.id)
        local = evaluate_gates(
            GateSet(
                name="simple",
                require_dataset_match=False,
                rules=[GateRule(metric_key="accuracy", minimum=0.9)],
            ),
            await service.load_metrics(uuid.UUID(run["id"])),
            None,
        )
        assert server["verdict"] == local.verdict.value == "pass"
        assert server["exit_code"] == local.exit_code == 0

    async def test_a_missing_metric_errors_on_both_sides(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """A typo'd metric key must never silently pass, in either place."""
        results = [result("ex-0", "accuracy", 1.0)]
        run = await self._seeded_run(client, tenant_a, results)
        gate_set = (
            await client.post(
                "/v1/quality-gate-sets",
                json={
                    "name": "typo",
                    "require_dataset_match": False,
                    "rules": [{"metric_key": "acccuracy", "minimum": 0.9}],
                },
                headers=auth(tenant_a),
            )
        ).json()

        server = (
            await client.post(
                "/v1/experiments/compare",
                json={"candidate_run_id": run["id"], "gate_set_id": gate_set["id"]},
                headers=auth(tenant_a),
            )
        ).json()
        service = ExperimentService(session, project_id=tenant_a.project.id)
        local = evaluate_gates(
            GateSet(
                name="typo",
                require_dataset_match=False,
                rules=[GateRule(metric_key="acccuracy", minimum=0.9)],
            ),
            await service.load_metrics(uuid.UUID(run["id"])),
            None,
        )
        assert server["verdict"] == local.verdict.value == "error"
        assert server["exit_code"] == local.exit_code == 2


class TestPolicies:
    VALID = "name: p\nrules:\n  - id: r\n    kind: forbidden_action\n    actions: [shell.exec]\n"

    async def test_validation_needs_no_persistence(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """Checking a policy before a run is far cheaper than after."""
        response = await client.post(
            "/v1/trajectory-policies/validate",
            json={"source_yaml": self.VALID},
            headers=auth(tenant_a),
        )
        assert response.status_code == 200
        assert response.json()["rule_count"] == 1

    async def test_an_invalid_policy_is_rejected_with_the_parser_message(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        bad = "name: p\nrules:\n  - id: r\n    kind: forbiden_action\n    actions: [x]\n"
        response = await client.post(
            "/v1/trajectory-policies/validate", json={"source_yaml": bad}, headers=auth(tenant_a)
        )
        assert response.status_code == 422
        assert "Did you mean" in response.json()["detail"]

    async def test_registering_the_same_policy_reuses_its_version(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        policy = (
            await client.post(
                "/v1/trajectory-policies",
                json={"name": "P", "slug": "p"},
                headers=auth(tenant_a),
            )
        ).json()
        first = (
            await client.post(
                f"/v1/trajectory-policies/{policy['id']}/versions",
                json={"source_yaml": self.VALID},
                headers=auth(tenant_a),
            )
        ).json()
        second = (
            await client.post(
                f"/v1/trajectory-policies/{policy['id']}/versions",
                json={"source_yaml": self.VALID},
                headers=auth(tenant_a),
            )
        ).json()
        assert first["version"] == second["version"] == 1


class TestPermissions:
    async def test_a_reviewer_cannot_lock_a_dataset(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """Locking a golden dataset is a curation act reserved for developers."""
        reviewer = await make_tenant(session, slug="rev", scopes=["read"])
        await session.flush()
        response = await client.post(
            "/v1/datasets", json={"name": "X", "slug": "x"}, headers=auth(reviewer)
        )
        assert response.status_code == 403

    async def test_an_ingest_key_cannot_create_experiments(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        ingest_only = await make_tenant(session, slug="ing", scopes=["ingest"])
        await session.flush()
        response = await client.post(
            "/v1/experiments", json={"name": "r", "suite_name": "s"}, headers=auth(ingest_only)
        )
        assert response.status_code == 403
