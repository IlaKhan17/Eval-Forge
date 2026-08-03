"""Dataset, evaluator, experiment, gate, and policy endpoints.

These are what the CLI talks to. The shapes are deliberately close to the CLI's own
domain objects so that neither side needs a translation layer that could drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from evalforge_api.api.dependencies import SessionDep, get_principal
from evalforge_api.db.models.evaluation import (
    Dataset,
    DatasetVersion,
    Evaluator,
    EvaluatorVersion,
    Experiment,
    QualityGateRule,
    QualityGateSet,
    TrajectoryPolicy,
    TrajectoryPolicyVersion,
)
from evalforge_api.errors import ConflictError, ForbiddenError, NotFoundError, UnprocessableError
from evalforge_api.security.permissions import Permission, Principal
from evalforge_api.services.datasets import DatasetService, config_hash
from evalforge_api.services.experiments import ExperimentService, slice_key
from evalforge_core.gates import GateReport
from evalforge_trajectory import PolicyError, load_policy
from evalforge_types import Example, ExampleResult, GateRule, Severity

router = APIRouter(prefix="/v1", tags=["evaluation"])


def _guard(permission: Permission) -> Any:
    async def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if principal.project_id is None:
            raise ForbiddenError("This endpoint requires a project-scoped credential.")
        if not principal.can(permission):
            raise ForbiddenError(f"This action requires the {permission.value!r} permission.")
        return principal

    return Depends(dependency)


Reader = Annotated[Principal, _guard(Permission.PROJECT_READ)]
Writer = Annotated[Principal, _guard(Permission.DATASET_WRITE)]
Runner = Annotated[Principal, _guard(Permission.EXPERIMENT_RUN)]


# ---------------------------------------------------------------------- schemas


class DatasetIn(BaseModel):
    name: str = Field(max_length=200)
    slug: str = Field(max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    kind: str = "general"
    description: str | None = None


class DatasetOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    kind: str


class VersionIn(BaseModel):
    version: str = Field(max_length=50)
    parent_version_id: uuid.UUID | None = None
    split: str | None = None
    notes: str | None = None


class VersionOut(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    version: str
    status: str
    example_count: int
    content_hash: str | None
    locked_at: datetime | None


class ExamplesIn(BaseModel):
    examples: list[Example] = Field(max_length=1000)


class EvaluatorIn(BaseModel):
    name: str = Field(max_length=200)
    slug: str = Field(max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    evaluator_type: str
    description: str | None = None


class EvaluatorVersionIn(BaseModel):
    config: dict[str, Any]
    judge_model: str | None = None
    judge_params: dict[str, Any] | None = None
    code_ref: str | None = None
    output_kind: str = "score"


class EvaluatorVersionOut(BaseModel):
    id: uuid.UUID
    evaluator_id: uuid.UUID
    version: int
    config_hash: str
    judge_model: str | None
    reused: bool = False


class ExperimentIn(BaseModel):
    name: str = Field(max_length=200)
    suite_name: str = Field(max_length=200)
    dataset_version_id: uuid.UUID | None = None
    task_ref: str | None = None
    task_version: str | None = None
    evaluator_version_ids: list[uuid.UUID] = Field(default_factory=list)
    policy_version_ids: list[uuid.UUID] = Field(default_factory=list)
    gate_set_id: uuid.UUID | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    git_dirty: bool = False
    dependency_lock_hash: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ExperimentOut(BaseModel):
    id: uuid.UUID
    name: str
    suite_name: str
    dataset_version_id: uuid.UUID | None
    dataset_content_hash: str | None
    git_commit: str | None
    git_branch: str | None
    is_baseline: bool


class RunOut(BaseModel):
    id: uuid.UUID
    experiment_id: uuid.UUID
    attempt: int
    status: str
    completed_examples: int
    failed_examples: int
    total_cost: float


class ResultsIn(BaseModel):
    results: list[ExampleResult] = Field(max_length=500)


class ResultsOut(BaseModel):
    stored: int
    skipped: int


class CompleteIn(BaseModel):
    status: str = "succeeded"
    error: str | None = None


class GateRuleIn(BaseModel):
    metric_key: str
    minimum: float | None = None
    maximum: float | None = None
    max_absolute_regression: float | None = None
    max_relative_regression: float | None = None
    blocking: bool = True
    slice: dict[str, str] | None = None
    require_baseline: bool = False


class GateSetIn(BaseModel):
    name: str = Field(max_length=200)
    rules: list[GateRuleIn]
    require_dataset_match: bool = True
    source_yaml: str | None = None


class CompareIn(BaseModel):
    candidate_run_id: uuid.UUID
    baseline_run_id: uuid.UUID | None = None
    # Resolve the baseline server-side when the client does not know it. This is what
    # lets a CI job say "compare against main" without querying for the run first.
    baseline_branch: str = "main"
    gate_set_id: uuid.UUID | None = None


class MetricDeltaOut(BaseModel):
    key: str
    slice: dict[str, str] | None
    baseline: float | None
    candidate: float | None
    absolute_delta: float | None
    relative_delta: float | None
    significant: bool | None


class GateResultOut(BaseModel):
    metric_key: str
    slice: dict[str, str] | None
    verdict: str
    severity: str
    rule: str | None
    threshold: float | None
    actual: float | None
    baseline: float | None
    message: str


class CompareOut(BaseModel):
    candidate_run_id: uuid.UUID
    baseline_run_id: uuid.UUID | None
    dataset_match: bool
    warnings: list[str]
    metrics: list[MetricDeltaOut]
    gates: list[GateResultOut] = Field(default_factory=list)
    verdict: str = "pass"
    exit_code: int = 0
    regressed_examples: list[dict[str, Any]] = Field(default_factory=list)


class PolicyIn(BaseModel):
    name: str = Field(max_length=200)
    slug: str = Field(max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str | None = None


class PolicyVersionIn(BaseModel):
    source_yaml: str


class PolicyVersionOut(BaseModel):
    id: uuid.UUID
    policy_id: uuid.UUID
    version: int
    content_hash: str
    rule_count: int


# --------------------------------------------------------------------- datasets


@router.post("/datasets", response_model=DatasetOut, status_code=status.HTTP_201_CREATED)
async def create_dataset(body: DatasetIn, session: SessionDep, principal: Writer) -> DatasetOut:
    existing = (
        await session.execute(
            select(Dataset).where(
                Dataset.project_id == principal.project_id,
                Dataset.slug == body.slug,
                Dataset.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"A dataset with slug {body.slug!r} already exists.")

    row = Dataset(
        project_id=principal.project_id,
        name=body.name,
        slug=body.slug,
        kind=body.kind,
        description=body.description,
    )
    session.add(row)
    await session.flush()
    return DatasetOut(id=row.id, name=row.name, slug=row.slug, kind=row.kind)


@router.get("/datasets", response_model=list[DatasetOut])
async def list_datasets(session: SessionDep, principal: Reader) -> list[DatasetOut]:
    rows = (
        (
            await session.execute(
                select(Dataset).where(
                    Dataset.project_id == principal.project_id, Dataset.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    return [DatasetOut(id=r.id, name=r.name, slug=r.slug, kind=r.kind) for r in rows]


@router.post(
    "/datasets/{dataset_id}/versions",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    dataset_id: uuid.UUID, body: VersionIn, session: SessionDep, principal: Writer
) -> VersionOut:
    dataset = await session.get(Dataset, dataset_id)
    if dataset is None or dataset.project_id != principal.project_id:
        raise NotFoundError("No such dataset.")

    row = DatasetVersion(
        project_id=principal.project_id,
        dataset_id=dataset_id,
        version=body.version,
        parent_version_id=body.parent_version_id,
        split=body.split,
        notes=body.notes,
    )
    session.add(row)
    await session.flush()
    return _version_out(row)


@router.post("/dataset-versions/{version_id}/examples", response_model=VersionOut)
async def append_examples(
    version_id: uuid.UUID, body: ExamplesIn, session: SessionDep, principal: Writer
) -> VersionOut:
    service = DatasetService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    await service.append_examples(version_id, body.examples)
    return _version_out(await service.get_version(version_id))


@router.get("/dataset-versions/{version_id}/examples", response_model=list[Example])
async def list_examples(
    version_id: uuid.UUID, session: SessionDep, principal: Reader
) -> list[Example]:
    service = DatasetService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    await service.get_version(version_id)
    return await service.load_examples(version_id)


@router.post("/dataset-versions/{version_id}/lock", response_model=VersionOut)
async def lock_version(version_id: uuid.UUID, session: SessionDep, principal: Writer) -> VersionOut:
    """Freeze the version and record its content hash. Idempotent."""
    if not principal.can(Permission.DATASET_LOCK):
        raise ForbiddenError("Locking a dataset requires the 'dataset.lock' permission.")
    service = DatasetService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    outcome = await service.lock(version_id)
    return _version_out(outcome.version)


@router.get("/dataset-versions/resolve", response_model=VersionOut)
async def resolve_version(
    dataset: str, version: str, session: SessionDep, principal: Reader
) -> VersionOut:
    """Resolve `slug` + label to a version, so suite files can use readable names."""
    service = DatasetService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    return _version_out(await service.resolve(dataset, version))


def _version_out(row: DatasetVersion) -> VersionOut:
    return VersionOut(
        id=row.id,
        dataset_id=row.dataset_id,
        version=row.version,
        status=row.status,
        example_count=row.example_count,
        content_hash=row.content_hash.hex() if row.content_hash else None,
        locked_at=row.locked_at,
    )


# ------------------------------------------------------------------- evaluators


@router.post("/evaluators", response_model=dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_evaluator(
    body: EvaluatorIn, session: SessionDep, principal: Writer
) -> dict[str, Any]:
    existing = (
        await session.execute(
            select(Evaluator).where(
                Evaluator.project_id == principal.project_id,
                Evaluator.slug == body.slug,
                Evaluator.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"id": str(existing.id), "slug": existing.slug, "created": False}

    row = Evaluator(
        project_id=principal.project_id,
        name=body.name,
        slug=body.slug,
        evaluator_type=body.evaluator_type,
        description=body.description,
    )
    session.add(row)
    await session.flush()
    return {"id": str(row.id), "slug": row.slug, "created": True}


@router.post(
    "/evaluators/{evaluator_id}/versions",
    response_model=EvaluatorVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_evaluator_version(
    evaluator_id: uuid.UUID, body: EvaluatorVersionIn, session: SessionDep, principal: Writer
) -> EvaluatorVersionOut:
    """Register a version, keyed by the hash of everything that affects a score.

    Re-registering an identical config returns the existing version rather than
    minting a new number. Otherwise every CI run would create a "new" evaluator
    version and comparison would refuse every pair as evaluator drift.
    """
    evaluator = await session.get(Evaluator, evaluator_id)
    if evaluator is None or evaluator.project_id != principal.project_id:
        raise NotFoundError("No such evaluator.")

    payload: dict[str, object] = {
        "config": body.config,
        "judge_model": body.judge_model,
        "judge_params": body.judge_params,
        "code_ref": body.code_ref,
        "output_kind": body.output_kind,
    }
    digest = config_hash(payload)

    existing = (
        await session.execute(
            select(EvaluatorVersion).where(
                EvaluatorVersion.evaluator_id == evaluator_id,
                EvaluatorVersion.config_hash == digest,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return EvaluatorVersionOut(
            id=existing.id,
            evaluator_id=evaluator_id,
            version=existing.version,
            config_hash=digest.hex(),
            judge_model=existing.judge_model,
            reused=True,
        )

    highest = (
        await session.execute(
            select(EvaluatorVersion.version)
            .where(EvaluatorVersion.evaluator_id == evaluator_id)
            .order_by(EvaluatorVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    row = EvaluatorVersion(
        project_id=principal.project_id,
        evaluator_id=evaluator_id,
        version=(highest or 0) + 1,
        config=body.config,
        config_hash=digest,
        judge_model=body.judge_model,
        judge_params=body.judge_params,
        code_ref=body.code_ref,
        output_kind=body.output_kind,
    )
    session.add(row)
    await session.flush()
    return EvaluatorVersionOut(
        id=row.id,
        evaluator_id=evaluator_id,
        version=row.version,
        config_hash=digest.hex(),
        judge_model=row.judge_model,
    )


# ------------------------------------------------------------------ experiments


@router.post("/experiments", response_model=ExperimentOut, status_code=status.HTTP_201_CREATED)
async def create_experiment(
    body: ExperimentIn, session: SessionDep, principal: Runner
) -> ExperimentOut:
    dataset_hash: bytes | None = None
    if body.dataset_version_id is not None:
        service = DatasetService(session, project_id=principal.project_id)  # type: ignore[arg-type]
        version = await service.get_version(body.dataset_version_id)
        if not version.is_locked:
            # An unlocked dataset can change under the experiment, which makes the
            # result unreproducible. The CLI can override with --allow-draft, but the
            # API refuses to record a reproducibility claim it cannot support.
            raise UnprocessableError(
                f"Dataset version {version.version!r} is a draft. Lock it before running an "
                "experiment, or the result cannot be reproduced."
            )
        dataset_hash = version.content_hash

    row = Experiment(
        project_id=principal.project_id,
        name=body.name,
        suite_name=body.suite_name,
        dataset_version_id=body.dataset_version_id,
        dataset_content_hash=dataset_hash,
        task_ref=body.task_ref,
        task_version=body.task_version,
        evaluator_version_ids=[str(i) for i in body.evaluator_version_ids],
        policy_version_ids=[str(i) for i in body.policy_version_ids],
        gate_set_id=body.gate_set_id,
        git_commit=body.git_commit,
        git_branch=body.git_branch,
        git_dirty=body.git_dirty,
        dependency_lock_hash=body.dependency_lock_hash,
        config=body.config,
    )
    session.add(row)
    await session.flush()
    return _experiment_out(row)


@router.post(
    "/experiments/{experiment_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED
)
async def open_run(
    experiment_id: uuid.UUID, session: SessionDep, principal: Runner, trigger: str = "cli"
) -> RunOut:
    service = ExperimentService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    return _run_out(await service.open_run(experiment_id, trigger=trigger))


@router.post("/experiment-runs/{run_id}/results", response_model=ResultsOut)
async def append_results(
    run_id: uuid.UUID, body: ResultsIn, session: SessionDep, principal: Runner
) -> ResultsOut:
    service = ExperimentService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    stored, skipped = await service.append_results(run_id, body.results)
    return ResultsOut(stored=stored, skipped=skipped)


@router.post("/experiment-runs/{run_id}/complete", response_model=RunOut)
async def complete_run(
    run_id: uuid.UUID, body: CompleteIn, session: SessionDep, principal: Runner
) -> RunOut:
    service = ExperimentService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    return _run_out(await service.complete_run(run_id, status=body.status, error=body.error))


@router.post("/experiment-runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: uuid.UUID, session: SessionDep, principal: Runner) -> RunOut:
    service = ExperimentService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    return _run_out(await service.cancel_run(run_id))


@router.get("/experiment-runs/{run_id}/metrics", response_model=list[dict[str, Any]])
async def run_metrics(
    run_id: uuid.UUID, session: SessionDep, principal: Reader
) -> list[dict[str, Any]]:
    service = ExperimentService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    await service.get_run(run_id)
    return [
        {
            "key": m.key,
            "slice": m.slice,
            "value": m.value,
            "count": m.count,
            "error_count": m.error_count,
            "ci_low": m.ci_low,
            "ci_high": m.ci_high,
        }
        for m in await service.load_metrics(run_id)
    ]


@router.post("/experiments/compare", response_model=CompareOut)
async def compare(body: CompareIn, session: SessionDep, principal: Reader) -> CompareOut:
    service = ExperimentService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    candidate = await service.get_run(body.candidate_run_id)

    baseline_id = body.baseline_run_id
    if baseline_id is None:
        experiment = await service.get_experiment(candidate.experiment_id)
        resolved = await service.resolve_baseline(
            suite_name=experiment.suite_name,
            branch=body.baseline_branch,
            exclude_run_id=candidate.id,
        )
        baseline_id = resolved.id if resolved else None

    comparison, dataset_match = await service.compare(candidate.id, baseline_id)

    report: GateReport | None = None
    if body.gate_set_id is not None:
        report = await service.evaluate_gates(
            gate_set_id=body.gate_set_id,
            candidate_run_id=candidate.id,
            baseline_run_id=baseline_id,
            dataset_match=dataset_match,
        )

    return CompareOut(
        candidate_run_id=candidate.id,
        baseline_run_id=baseline_id,
        dataset_match=dataset_match,
        warnings=comparison.warnings,
        metrics=[
            MetricDeltaOut(
                key=d.key,
                slice=d.slice,
                baseline=d.baseline,
                candidate=d.candidate,
                absolute_delta=d.absolute_delta,
                relative_delta=d.relative_delta,
                significant=d.significant,
            )
            for d in comparison.deltas
        ],
        gates=[
            GateResultOut(
                metric_key=g.metric_key,
                slice=g.slice,
                verdict=g.verdict,
                severity=g.severity.value,
                rule=g.rule,
                threshold=g.threshold,
                actual=g.actual,
                baseline=g.baseline,
                message=g.message,
            )
            for g in (report.results if report else [])
        ],
        verdict=report.verdict.value if report else "pass",
        exit_code=report.exit_code if report else 0,
        regressed_examples=[
            {
                "example_id": r.example_id,
                "metric": r.metric,
                "baseline_score": r.baseline_score,
                "candidate_score": r.candidate_score,
                "trace_id": r.trace_id,
            }
            for r in comparison.regressions[:50]
        ],
    )


@router.post("/experiments/{experiment_id}/promote-baseline", response_model=ExperimentOut)
async def promote_baseline(
    experiment_id: uuid.UUID, session: SessionDep, principal: Runner, label: str = "default"
) -> ExperimentOut:
    """Mark an experiment as the baseline for its suite.

    Curation, not a data change, which is why it is the one mutable field on an
    otherwise immutable record.
    """
    service = ExperimentService(session, project_id=principal.project_id)  # type: ignore[arg-type]
    experiment = await service.get_experiment(experiment_id)

    previous = (
        (
            await session.execute(
                select(Experiment).where(
                    Experiment.project_id == principal.project_id,
                    Experiment.suite_name == experiment.suite_name,
                    Experiment.is_baseline.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in previous:
        row.is_baseline = False

    experiment.is_baseline = True
    experiment.baseline_label = label
    await session.flush()
    return _experiment_out(experiment)


def _experiment_out(row: Experiment) -> ExperimentOut:
    return ExperimentOut(
        id=row.id,
        name=row.name,
        suite_name=row.suite_name,
        dataset_version_id=row.dataset_version_id,
        dataset_content_hash=row.dataset_content_hash.hex() if row.dataset_content_hash else None,
        git_commit=row.git_commit,
        git_branch=row.git_branch,
        is_baseline=row.is_baseline,
    )


def _run_out(row: Any) -> RunOut:
    return RunOut(
        id=row.id,
        experiment_id=row.experiment_id,
        attempt=row.attempt,
        status=row.status,
        completed_examples=row.completed_examples,
        failed_examples=row.failed_examples,
        total_cost=float(row.total_cost),
    )


# ------------------------------------------------------------ gates and policies


@router.post(
    "/quality-gate-sets", response_model=dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_gate_set(
    body: GateSetIn, session: SessionDep, principal: Writer
) -> dict[str, Any]:
    highest = (
        await session.execute(
            select(QualityGateSet.version)
            .where(
                QualityGateSet.project_id == principal.project_id,
                QualityGateSet.name == body.name,
            )
            .order_by(QualityGateSet.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    gate_set = QualityGateSet(
        project_id=principal.project_id,
        name=body.name,
        version=(highest or 0) + 1,
        source_yaml=body.source_yaml,
        require_dataset_match=body.require_dataset_match,
    )
    session.add(gate_set)
    await session.flush()

    for rule in body.rules:
        # Validate through the shared GateRule model, so a gate the CLI would reject
        # cannot be stored server-side and quietly behave differently.
        validated = GateRule(
            metric_key=rule.metric_key,
            minimum=rule.minimum,
            maximum=rule.maximum,
            max_absolute_regression=rule.max_absolute_regression,
            max_relative_regression=rule.max_relative_regression,
            severity=Severity.BLOCK if rule.blocking else Severity.WARN,
            slice=rule.slice,
            require_baseline=rule.require_baseline,
        )
        session.add(
            QualityGateRule(
                project_id=principal.project_id,
                gate_set_id=gate_set.id,
                metric_key=validated.metric_key,
                minimum=validated.minimum,
                maximum=validated.maximum,
                max_absolute_regression=validated.max_absolute_regression,
                max_relative_regression=validated.max_relative_regression,
                severity=validated.severity.value,
                slice=validated.slice,
                slice_key=slice_key(validated.slice),
                require_baseline=validated.require_baseline,
                max_error_rate=validated.max_error_rate,
            )
        )
    await session.flush()
    return {"id": str(gate_set.id), "name": gate_set.name, "version": gate_set.version}


@router.post(
    "/trajectory-policies", response_model=dict[str, Any], status_code=status.HTTP_201_CREATED
)
async def create_policy(body: PolicyIn, session: SessionDep, principal: Writer) -> dict[str, Any]:
    existing = (
        await session.execute(
            select(TrajectoryPolicy).where(
                TrajectoryPolicy.project_id == principal.project_id,
                TrajectoryPolicy.slug == body.slug,
                TrajectoryPolicy.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"id": str(existing.id), "slug": existing.slug, "created": False}

    row = TrajectoryPolicy(
        project_id=principal.project_id,
        name=body.name,
        slug=body.slug,
        description=body.description,
    )
    session.add(row)
    await session.flush()
    return {"id": str(row.id), "slug": row.slug, "created": True}


@router.post("/trajectory-policies/validate", response_model=dict[str, Any])
async def validate_policy(body: PolicyVersionIn, principal: Reader) -> dict[str, Any]:  # noqa: ARG001 — auth guard
    """Dry-run validation with no persistence.

    Lets the CLI check a policy before a run, which is much cheaper than discovering
    a parse error after paying for the task execution.
    """
    try:
        loaded = load_policy(body.source_yaml)
    except PolicyError as exc:
        raise UnprocessableError(str(exc)) from exc
    return {
        "valid": True,
        "name": loaded.policy.name,
        "rule_count": len(loaded.policy.rules),
        "content_hash": loaded.content_hash,
    }


@router.post(
    "/trajectory-policies/{policy_id}/versions",
    response_model=PolicyVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_policy_version(
    policy_id: uuid.UUID, body: PolicyVersionIn, session: SessionDep, principal: Writer
) -> PolicyVersionOut:
    policy = await session.get(TrajectoryPolicy, policy_id)
    if policy is None or policy.project_id != principal.project_id:
        raise NotFoundError("No such policy.")

    try:
        loaded = load_policy(body.source_yaml)
    except PolicyError as exc:
        # Rejecting at registration is far cheaper than at run time, and a policy
        # that fails to parse mid-run has already cost the whole task execution.
        raise UnprocessableError(str(exc)) from exc

    digest = bytes.fromhex(loaded.content_hash.ljust(64, "0")[:64])
    existing = (
        await session.execute(
            select(TrajectoryPolicyVersion).where(
                TrajectoryPolicyVersion.policy_id == policy_id,
                TrajectoryPolicyVersion.content_hash == digest,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return PolicyVersionOut(
            id=existing.id,
            policy_id=policy_id,
            version=existing.version,
            content_hash=digest.hex(),
            rule_count=len(loaded.policy.rules),
        )

    highest = (
        await session.execute(
            select(TrajectoryPolicyVersion.version)
            .where(TrajectoryPolicyVersion.policy_id == policy_id)
            .order_by(TrajectoryPolicyVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    row = TrajectoryPolicyVersion(
        project_id=principal.project_id,
        policy_id=policy_id,
        version=(highest or 0) + 1,
        source_yaml=body.source_yaml,
        parsed=loaded.policy.model_dump(mode="json"),
        content_hash=digest,
    )
    session.add(row)
    await session.flush()
    return PolicyVersionOut(
        id=row.id,
        policy_id=policy_id,
        version=row.version,
        content_hash=digest.hex(),
        rule_count=len(loaded.policy.rules),
    )
