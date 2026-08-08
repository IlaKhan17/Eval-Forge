"""Send a completed run to the server.

Publishing is what turns a CI job's exit code into a record: the dataset it ran against, the
examples and their scores, the gate verdict, and the comparison to the baseline — all durable, all
tied to a commit. Without it the run exists for as long as the CI log is retained, and the dashboard
can only show production traffic, never the history of what CI decided.

Four rules this module is built on, in order of importance:

1. **Publishing never changes the verdict.** The exit code comes from the local evaluation, which
   already ran. A server that is slow, unreachable, or misconfigured must not be able to turn a
   failing run into a passing one — or a passing one into a failure — because that would make the
   gate depend on infrastructure rather than on the code being merged.

2. **A failure to publish is reported, never swallowed.** The whole point is a durable record; a
   run that silently did not produce one is worse than an obvious error, because nobody looks for
   the thing they believe exists. `--require-publish` escalates it to a hard failure for teams whose
   process depends on the record.

3. **Dataset versions are content-addressed.** The version label is derived from the content hash,
   so identical data always resolves to the same version and changed data always creates a new one.
   That is what makes `dataset_match` mean something: a comparison across two different datasets is
   refused rather than quietly reported.

4. **The server's verdict is checked against the local one.** They are computed by the same code
   from the same numbers, so they must agree — and if they ever do not, that is the single most
   important bug in the system, because the exit code CI acted on and the verdict the dashboard
   shows would have disagreed. `apps/api/tests/test_parity.py` guards this from the other side;
   this check is the runtime backstop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from evalforge_types import Metric

if TYPE_CHECKING:
    from evalforge_cli.suite.loader import LoadedSuite
    from evalforge_core.dataset import Dataset
    from evalforge_core.runner import EvalResult

#: Batch sizes the API accepts. Enforced there by the wire models; mirrored here so a large suite is
#: chunked rather than rejected.
EXAMPLES_PER_REQUEST = 1_000
RESULTS_PER_REQUEST = 500

DEFAULT_TIMEOUT = 60.0


class PublishError(RuntimeError):
    """Publishing failed. Never raised out of `publish()` — carried on the outcome instead."""


@dataclass
class PublishOutcome:
    """What publishing did, or why it did not happen.

    Returned rather than raised because the caller has already computed a verdict and must report it
    regardless. Every field here is designed to be printed: a publish outcome nobody can read is the
    same as no record at all.
    """

    published: bool = False
    skipped_reason: str | None = None
    error: str | None = None

    experiment_id: str | None = None
    run_id: str | None = None
    experiment_url: str | None = None
    dataset_version_id: str | None = None
    baseline_run_id: str | None = None

    server_verdict: str | None = None
    server_exit_code: int | None = None
    #: Ways the server's answer differed from the local one. Non-empty means a real bug, not a
    #: configuration problem — see rule 4 in the module docstring.
    divergences: list[str] = field(default_factory=list)


def slugify(value: str) -> str:
    """A dataset slug the API will accept: lowercase, alphanumeric, hyphen-separated.

    Derived from the suite's dataset reference rather than asked for in the suite file, because a
    slug is an implementation detail of the server and making people invent one is a step that adds
    no information.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "dataset")[:100]


def version_label(content_hash: str) -> str:
    """Content-addressed version label.

    `sha-<12 hex>` rather than an incrementing number or a timestamp. Two runs over identical data
    resolve to the same version — so a baseline comparison is against the same examples, provably —
    and changed data cannot reuse a label, which is the failure that makes a "regression" actually a
    dataset edit nobody noticed.
    """
    return f"sha-{content_hash[:12]}"


class Publisher:
    """One run's worth of HTTP calls, in the order they have to happen."""

    def __init__(self, endpoint: str, api_key: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._client = httpx.Client(
            base_url=self.endpoint,
            headers={"authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Publisher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ plumbing

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            msg = f"{method} {path}: {type(exc).__name__}: {exc}"
            raise PublishError(msg) from exc
        if response.status_code >= 400:
            # The API's error bodies are RFC 9457 problem documents; `detail` is the sentence
            # written for a human, and quoting it beats reprinting a status code.
            detail = ""
            try:
                detail = str(response.json().get("detail") or response.text)
            except ValueError:
                detail = response.text
            msg = f"{method} {path} → {response.status_code}: {detail[:300]}"
            raise PublishError(msg)
        return response.json() if response.content else None

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._request("POST", path, **kwargs)

    # ------------------------------------------------------------------- dataset

    def ensure_dataset(self, *, name: str, slug: str) -> str:
        """Create the dataset, or find the one that is already there.

        The API refuses a duplicate slug with 409 rather than returning the existing row, which is
        right for an API — a silent "created" for something that existed hides a mistake — and means
        the client is responsible for the read-then-create. The read comes second: creating first
        and catching the conflict is one round trip in the common case rather than two, and it has
        no race worth worrying about because the loser reads the winner's row.
        """
        try:
            created = self.post("/v1/datasets", json={"name": name, "slug": slug, "kind": "golden"})
        except PublishError as exc:
            if "409" not in str(exc):
                raise
        else:
            return str(created["id"])

        for dataset in self.get("/v1/datasets"):
            if dataset["slug"] == slug:
                return str(dataset["id"])
        msg = f"dataset {slug!r} could not be created and could not be found"
        raise PublishError(msg)

    def ensure_version(self, *, dataset_id: str, slug: str, dataset: Dataset) -> str:
        """A locked dataset version whose content hash matches the local data.

        Resolved by content-addressed label first, so a repeat run of the same suite over unchanged
        data reuses the version rather than uploading it again — which for a large dataset is the
        difference between a publish that takes a second and one that takes a minute.
        """
        local_hash = dataset.content_hash
        label = version_label(local_hash)

        try:
            existing = self.get(
                "/v1/dataset-versions/resolve", params={"dataset": slug, "version": label}
            )
        except PublishError as exc:
            if "404" not in str(exc):
                raise
        else:
            self._assert_hash(existing, local_hash, label)
            return str(existing["id"])

        version = self.post(f"/v1/datasets/{dataset_id}/versions", json={"version": label})
        version_id = str(version["id"])

        examples = list(dataset)
        for start in range(0, len(examples), EXAMPLES_PER_REQUEST):
            chunk = examples[start : start + EXAMPLES_PER_REQUEST]
            self.post(
                f"/v1/dataset-versions/{version_id}/examples",
                json={"examples": [example.model_dump(mode="json") for example in chunk]},
            )

        locked = self.post(f"/v1/dataset-versions/{version_id}/lock", json={})
        self._assert_hash(locked, local_hash, label)
        return version_id

    def _assert_hash(self, version: dict[str, Any], local_hash: str, label: str) -> None:
        """Refuse to continue when the server's hash of the same data differs.

        This should be impossible — both sides call `evalforge_types.content_hash` over the same
        examples — which is exactly why it is worth checking. If canonicalisation ever drifts, every
        downstream comparison silently becomes a comparison across different data, and the
        `dataset_match` guard that exists to prevent that would itself be lying.
        """
        remote = version.get("content_hash")
        if remote and remote != local_hash:
            msg = (
                f"dataset hash mismatch on version {label!r}: the server computed {remote[:12]}… "
                f"and this run computed {local_hash[:12]}…. Publishing would record a comparison "
                "across different data. This is a bug in canonicalisation, not a configuration "
                "problem."
            )
            raise PublishError(msg)

    # ---------------------------------------------------------------- gate set

    def ensure_gate_set(self, loaded: LoadedSuite) -> str | None:
        """Mirror the suite's gates server-side, so the server gates on what the repository says.

        Sent as the shared `GateRule` shape — severity included. The server used to have no wire
        representation for `severity`, which turned every warning into a blocking rule; that is
        fixed, and `apps/api/tests/test_parity.py` now guards the whole model.
        """
        from evalforge_cli.runner import build_gate_set  # noqa: PLC0415 — avoids a cycle

        gate_set = build_gate_set(loaded)
        if gate_set is None or not gate_set.rules:
            # A suite with no gates still publishes its results — the record is worth having even
            # when nothing is being enforced yet, and that is a common way to adopt this.
            return None

        created = self.post(
            "/v1/quality-gate-sets",
            json={
                "name": loaded.suite.name,
                "require_dataset_match": gate_set.require_dataset_match,
                "require_calibration": (
                    gate_set.require_calibration
                    if isinstance(gate_set.require_calibration, bool)
                    else gate_set.require_calibration.model_dump(mode="json")
                ),
                "rules": [
                    {
                        "metric_key": rule.metric_key,
                        "minimum": rule.minimum,
                        "maximum": rule.maximum,
                        "max_absolute_regression": rule.max_absolute_regression,
                        "max_relative_regression": rule.max_relative_regression,
                        "severity": rule.severity.value,
                        "slice": rule.slice,
                        "require_baseline": rule.require_baseline,
                        "max_error_rate": rule.max_error_rate,
                    }
                    for rule in gate_set.rules
                ],
            },
        )
        return str(created["id"])

    # -------------------------------------------------------------- experiment

    def record_run(
        self,
        *,
        loaded: LoadedSuite,
        result: EvalResult,
        dataset_version_id: str,
        gate_set_id: str | None,
        git: tuple[str | None, str | None, bool],
    ) -> tuple[str, str]:
        commit, branch, dirty = git
        experiment = self.post(
            "/v1/experiments",
            json={
                "name": loaded.suite.name,
                "suite_name": loaded.suite.name,
                "dataset_version_id": dataset_version_id,
                "gate_set_id": gate_set_id,
                "task_ref": loaded.suite.task.entrypoint if loaded.suite.task else None,
                "git_commit": commit,
                "git_branch": branch,
                # Recorded, not refused. A dirty tree is normal locally and meaningful in CI, and
                # the honest move is to say so on the record rather than to reject the run.
                "git_dirty": dirty,
            },
        )
        experiment_id = str(experiment["id"])

        run = self.post(f"/v1/experiments/{experiment_id}/runs", params={"trigger": "cli"})
        run_id = str(run["id"])

        for start in range(0, len(result.results), RESULTS_PER_REQUEST):
            chunk = result.results[start : start + RESULTS_PER_REQUEST]
            self.post(
                f"/v1/experiment-runs/{run_id}/results",
                json={"results": [row.model_dump(mode="json") for row in chunk]},
            )

        # Completing is what triggers server-side aggregation. A run left open has results and no
        # metrics, which reads as "the suite produced nothing".
        status = "failed" if result.aborted_reason else "succeeded"
        self.post(
            f"/v1/experiment-runs/{run_id}/complete",
            json={"status": status, "error": result.aborted_reason},
        )

        # After completing, never before: completing recomputes the run's aggregates from the stored
        # scores and clears what was there, so metrics submitted first would be deleted.
        #
        # Everything is offered; the server keeps only what it could not compute for itself and
        # names the rest. That split is deliberate — see `ExperimentService.submit_metrics`. Without
        # this call a suite that gates on a corpus metric (a protected class's recall, p95 latency)
        # publishes and then reads as ERROR on the server, because those metrics cannot be derived
        # from per-example scores by anyone but the process that ran the suite.
        self.post(
            f"/v1/experiment-runs/{run_id}/metrics",
            json={"metrics": [metric.model_dump(mode="json") for metric in result.metrics]},
        )
        return experiment_id, run_id

    def fetch_baseline(self, *, suite_name: str, branch: str) -> dict[str, Any]:
        return dict(
            self.get(
                "/v1/experiments/baseline",
                params={"suite_name": suite_name, "branch": branch},
            )
        )

    def compare(
        self, *, run_id: str, gate_set_id: str | None, baseline_branch: str
    ) -> dict[str, Any]:
        return dict(
            self.post(
                "/v1/experiments/compare",
                json={
                    "candidate_run_id": run_id,
                    "gate_set_id": gate_set_id,
                    "baseline_branch": baseline_branch,
                },
            )
        )


def publish(
    loaded: LoadedSuite,
    result: EvalResult,
    dataset: Dataset,
    *,
    endpoint: str,
    api_key: str,
    git: tuple[str | None, str | None, bool],
    timeout: float = DEFAULT_TIMEOUT,
) -> PublishOutcome:
    """Record this run on the server. Never raises; failures land on the outcome.

    Deliberately not async and not concurrent. Every step here depends on the previous one's id, so
    concurrency would buy nothing, and this runs after an evaluation that already took the time it
    took.
    """
    outcome = PublishOutcome()
    try:
        with Publisher(endpoint, api_key, timeout=timeout) as publisher:
            slug = slugify(loaded.suite.dataset.name or loaded.suite.name)
            dataset_id = publisher.ensure_dataset(name=loaded.suite.name, slug=slug)
            version_id = publisher.ensure_version(dataset_id=dataset_id, slug=slug, dataset=dataset)
            outcome.dataset_version_id = version_id

            gate_set_id = publisher.ensure_gate_set(loaded)
            experiment_id, run_id = publisher.record_run(
                loaded=loaded,
                result=result,
                dataset_version_id=version_id,
                gate_set_id=gate_set_id,
                git=git,
            )
            outcome.experiment_id = experiment_id
            outcome.run_id = run_id
            outcome.experiment_url = f"{publisher.endpoint}/v1/experiments/{experiment_id}"

            compared = publisher.compare(
                run_id=run_id,
                gate_set_id=gate_set_id,
                baseline_branch=loaded.suite.baseline.branch,
            )
            outcome.baseline_run_id = compared.get("baseline_run_id")
            outcome.server_verdict = compared.get("verdict")
            outcome.server_exit_code = compared.get("exit_code")
            outcome.divergences = _divergences(result, compared)
            outcome.published = True
    except PublishError as exc:
        outcome.error = str(exc)
    except Exception as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome


def _divergences(result: EvalResult, compared: dict[str, Any]) -> list[str]:
    """Where the server's answer differs from the one this process already reported.

    A first run has no baseline, so regression rules skip on both sides and the verdicts match. When
    a baseline *does* exist server-side and did not locally, the server can legitimately reach a
    stricter verdict — that is not a divergence, it is the server knowing more, so it is reported as
    context rather than as a disagreement.
    """
    notes: list[str] = []
    server_verdict = compared.get("verdict")
    local_verdict = result.gates.verdict.value

    if server_verdict is None:
        return notes

    if server_verdict != local_verdict:
        extra = (
            " (the server resolved a baseline this run did not have, so a regression rule it "
            "could apply was skipped here)"
            if compared.get("baseline_run_id")
            else ""
        )
        notes.append(
            f"verdict differs: this run reported {local_verdict!r}, the server {server_verdict!r}"
            f"{extra}"
        )
    return notes


@dataclass
class Baseline:
    """The run this one will be measured against, or an explicit absence."""

    run_id: str | None = None
    git_commit: str | None = None
    metrics: list[Metric] = field(default_factory=list)
    error: str | None = None

    @property
    def label(self) -> str | None:
        if self.run_id is None:
            return None
        return f"{self.git_commit[:7]} ({self.run_id[:8]})" if self.git_commit else self.run_id[:8]


def fetch_baseline(
    loaded: LoadedSuite, *, endpoint: str, api_key: str, timeout: float = DEFAULT_TIMEOUT
) -> Baseline:
    """Pull the baseline's metrics so regression gates can be applied locally.

    Before the run, not after. A regression rule the local evaluation had to skip is a rule the
    server then applies during `compare`, which produces a verdict difference that is legitimate and
    indistinguishable from the kind that means something is broken. Fetching first collapses that
    ambiguity: both sides see the same baseline and must agree.

    Never raises. No baseline is the normal state of a new suite, and an unreachable server must
    degrade to "no regression comparison" rather than stopping a run that can still gate on its
    absolute floors.
    """
    if loaded.suite.baseline.strategy == "none":
        return Baseline()
    try:
        with Publisher(endpoint, api_key, timeout=timeout) as publisher:
            payload = publisher.fetch_baseline(
                suite_name=loaded.suite.name, branch=loaded.suite.baseline.branch
            )
    except PublishError as exc:
        return Baseline(error=str(exc))
    except Exception as exc:
        return Baseline(error=f"{type(exc).__name__}: {exc}")

    if payload.get("run_id") is None:
        return Baseline()
    return Baseline(
        run_id=str(payload["run_id"]),
        git_commit=payload.get("git_commit"),
        metrics=[Metric.model_validate(row) for row in payload.get("metrics") or []],
    )


__all__ = [
    "Baseline",
    "PublishError",
    "PublishOutcome",
    "Publisher",
    "fetch_baseline",
    "publish",
    "slugify",
    "version_label",
]
