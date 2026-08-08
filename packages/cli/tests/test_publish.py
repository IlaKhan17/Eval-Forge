"""Publishing, against a stub server.

The behaviour worth pinning here is not "it sends the right JSON" — an integration test proves that
against the real API. It is the set of promises publishing makes to a CI job:

- it never changes the verdict, and never raises into a run that already finished;
- it refuses to record a comparison across different data;
- it reuses a dataset version rather than re-uploading identical examples;
- it says something when it does not happen.

Each of those is a decision that a reasonable implementation could get wrong in a way no smoke test
would notice, because the failure mode is a *missing* record or a *quietly wrong* comparison.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evalforge_cli import publish as publish_module
from evalforge_cli.runner import execute
from evalforge_cli.suite.loader import load_suite
from evalforge_core.dataset import Dataset
from evalforge_types import Example

ROOT = Path(__file__).resolve().parents[3]
SUITE = ROOT / "evals" / "suites" / "reply-intent.yaml"


@pytest.fixture
def loaded() -> Any:
    return load_suite(SUITE)


@pytest.fixture
def result(loaded: Any) -> Any:
    import asyncio

    return asyncio.run(execute(loaded, limit=4)).result


def dataset() -> Dataset:
    return Dataset(
        [
            Example(id="e1", input={"body": "remove me"}, expected={"intent": "unsubscribe"}),
            Example(id="e2", input={"body": "meet tuesday"}, expected={"intent": "meeting"}),
        ]
    )


class Server:
    """A stub API that records what it was asked to do.

    Hand-written rather than mocked: the sequence of calls *is* the contract, and a mock that
    asserts on call order tends to encode the implementation instead of the promise.
    """

    def __init__(self, **behaviour: Any) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: dict[str, Any] = {}
        self.behaviour = behaviour
        self.example_batches: list[int] = []
        self.result_batches: list[int] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        self.bodies[f"{request.method} {path}"] = body

        if path == "/v1/datasets" and request.method == "POST":
            if self.behaviour.get("dataset_exists"):
                return httpx.Response(409, json={"detail": "A dataset with that slug exists."})
            return httpx.Response(201, json={"id": "ds-1"})
        if path == "/v1/datasets":
            return httpx.Response(200, json=[{"id": "ds-1", "slug": self.behaviour.get("slug")}])

        if path == "/v1/dataset-versions/resolve":
            if self.behaviour.get("version_exists"):
                return httpx.Response(
                    200, json={"id": "dv-1", "content_hash": self.behaviour.get("remote_hash")}
                )
            return httpx.Response(404, json={"detail": "No such version."})

        if path.endswith("/versions") and request.method == "POST":
            return httpx.Response(201, json={"id": "dv-1"})
        if path.endswith("/examples"):
            self.example_batches.append(len(body["examples"]))
            return httpx.Response(200, json={"id": "dv-1"})
        if path.endswith("/lock"):
            return httpx.Response(
                200, json={"id": "dv-1", "content_hash": self.behaviour.get("remote_hash")}
            )

        if path == "/v1/quality-gate-sets":
            return httpx.Response(201, json={"id": "gs-1"})
        if path == "/v1/experiments" and request.method == "POST":
            return httpx.Response(201, json={"id": "exp-1"})
        if path.endswith("/runs"):
            return httpx.Response(201, json={"id": "run-1"})
        if path.endswith("/results"):
            self.result_batches.append(len(body["results"]))
            return httpx.Response(200, json={"stored": len(body["results"]), "skipped": 0})
        if path.endswith("/complete"):
            return httpx.Response(200, json={"id": "run-1"})
        if path.endswith("/metrics"):
            return httpx.Response(200, json={"stored": len(body["metrics"]), "rejected": []})
        if path == "/v1/experiments/compare":
            return httpx.Response(200, json=self.behaviour.get("compare", {"verdict": "pass"}))
        if path == "/v1/experiments/baseline":
            return httpx.Response(200, json=self.behaviour.get("baseline", {"run_id": None}))

        return httpx.Response(404, json={"detail": f"unhandled {path}"})


def publisher_for(server: Server) -> publish_module.Publisher:
    publisher = publish_module.Publisher("http://server", "ef_test_key")
    publisher._client = httpx.Client(
        base_url="http://server",
        transport=server.transport(),
        headers={"authorization": "Bearer ef_test_key"},
    )
    return publisher


class TestDatasetVersions:
    def test_the_version_label_is_content_addressed(self) -> None:
        """Identical data must resolve to the same version, changed data to a different one.

        This is what makes `dataset_match` mean anything. A timestamped or incrementing label would
        let two different datasets share a version, and every comparison across them would be
        reported as a regression rather than refused.
        """
        first = publish_module.version_label("a" * 64)
        assert first == publish_module.version_label("a" * 64)
        assert first != publish_module.version_label("b" * 64)
        assert first.startswith("sha-")

    def test_identical_data_is_not_uploaded_twice(self) -> None:
        data = dataset()
        server = Server(version_exists=True, remote_hash=data.content_hash)
        with publisher_for(server) as publisher:
            version_id = publisher.ensure_version(dataset_id="ds-1", slug="d", dataset=data)

        assert version_id == "dv-1"
        assert server.example_batches == [], "the examples were re-uploaded for an existing version"

    def test_a_server_hash_that_disagrees_stops_the_publish(self) -> None:
        """The check that should never fire, which is why it is worth having.

        Both sides hash the same examples with the same function. If they ever disagree, every
        downstream comparison is silently across different data — and `dataset_match`, the guard
        that exists to catch exactly that, would itself be wrong.
        """
        data = dataset()
        server = Server(version_exists=True, remote_hash="f" * 64)
        with publisher_for(server) as publisher, pytest.raises(publish_module.PublishError) as exc:
            publisher.ensure_version(dataset_id="ds-1", slug="d", dataset=data)

        assert "hash mismatch" in str(exc.value)

    def test_an_existing_dataset_is_reused_rather_than_failing(self) -> None:
        # The API refuses a duplicate slug with 409, which is right for an API and means the client
        # has to handle it. A CI job that published once must not fail on its second run.
        server = Server(dataset_exists=True, slug="reply-intent")
        with publisher_for(server) as publisher:
            assert publisher.ensure_dataset(name="Reply intent", slug="reply-intent") == "ds-1"


class TestPublishNeverBreaksTheRun:
    def test_an_unreachable_server_is_reported_not_raised(self, loaded: Any, result: Any) -> None:
        """Rule one: a completed run has already produced a verdict.

        If publishing could raise, an unreachable server would turn a passing run into a crash — and
        the gate would depend on infrastructure rather than on the code being merged.
        """
        outcome = publish_module.publish(
            loaded,
            result,
            dataset(),
            # Port 1 is reserved and refuses immediately, so the test does not wait on a timeout.
            endpoint="http://127.0.0.1:1",
            api_key="ef_test_key",
            git=(None, None, False),
            timeout=1.0,
        )
        assert outcome.published is False
        assert outcome.error, "an unreachable server must say so"

    def test_a_rejected_request_names_the_endpoint_and_the_reason(self) -> None:
        server = Server()
        with publisher_for(server) as publisher, pytest.raises(publish_module.PublishError) as exc:
            publisher.get("/v1/nonexistent")
        message = str(exc.value)
        assert "/v1/nonexistent" in message
        assert "404" in message


class TestVerdictAgreement:
    def test_a_matching_verdict_produces_no_divergence(self, loaded: Any, result: Any) -> None:
        assert publish_module._divergences(result, {"verdict": result.gates.verdict.value}) == []

    def test_a_differing_verdict_is_reported(self, loaded: Any, result: Any) -> None:
        # The single most important bug this system can have: the exit code CI acted on and the
        # verdict the dashboard shows disagree, computed by the same code from the same numbers.
        notes = publish_module._divergences(result, {"verdict": "fail"})
        assert notes
        assert "verdict differs" in notes[0]

    def test_a_server_side_baseline_is_explained_rather_than_blamed(
        self, loaded: Any, result: Any
    ) -> None:
        """Not every difference is a bug.

        When the server resolved a baseline this run did not have, it can apply a regression rule
        the local evaluation had to skip. That is the server knowing more, and reporting it as a
        disagreement would train people to ignore the message that matters.
        """
        notes = publish_module._divergences(result, {"verdict": "fail", "baseline_run_id": "run-0"})
        assert notes
        assert "resolved a baseline" in notes[0]


class TestBaseline:
    def test_no_baseline_is_a_normal_answer(self, loaded: Any) -> None:
        # The first run of a new suite. Returning an error here would make "nothing to compare
        # against" indistinguishable from "the lookup broke".
        server = Server(baseline={"run_id": None})
        publisher = publisher_for(server)
        payload = publisher.fetch_baseline(suite_name="reply-intent", branch="main")
        publisher.close()
        assert payload["run_id"] is None

    def test_an_unreachable_server_degrades_to_no_baseline(self, loaded: Any) -> None:
        # Absolute floors still gate correctly without a baseline; only regression rules are
        # skipped. Failing the run instead would make every gate depend on the server being up.
        baseline = publish_module.fetch_baseline(
            loaded, endpoint="http://127.0.0.1:1", api_key="ef_test_key", timeout=1.0
        )
        assert baseline.run_id is None
        assert baseline.error
        assert baseline.metrics == []


class TestBatching:
    def test_results_are_chunked_to_the_limit_the_api_accepts(self) -> None:
        """A suite larger than one request must publish, not 422.

        The API caps a results batch at 500 in its wire model. Discovering that at publish time,
        after a long run, is the worst moment to discover it.
        """
        from evalforge_core.runner import EvalResult
        from evalforge_types import ExampleResult

        server = Server()
        result = EvalResult(
            suite="big",
            results=[ExampleResult(example_id=f"e{i}") for i in range(1_200)],
        )
        with publisher_for(server) as publisher:
            publisher.record_run(
                loaded=load_suite(SUITE),
                result=result,
                dataset_version_id="dv-1",
                gate_set_id=None,
                git=(None, None, False),
            )
        assert server.result_batches == [500, 500, 200]
