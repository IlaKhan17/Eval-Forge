"""Experiment runs, aggregation, comparison, and gate evaluation.

The load-bearing property: **the server reaches the same verdict as the CLI.** It
does so by calling the identical `evaluation-core` functions rather than
reimplementing them. A second implementation of the gate engine would drift, and the
day the CI exit code disagrees with the dashboard is the day nobody trusts either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from proofstep_api.db.models.evaluation import (
    AggregateMetric,
    EvaluationResult,
    Experiment,
    ExperimentResult,
    ExperimentRun,
    QualityGateRule,
    QualityGateSet,
)
from proofstep_api.errors import ConflictError, NotFoundError
from proofstep_core.aggregate import aggregate_scores
from proofstep_core.compare import Comparison, compare_metrics
from proofstep_core.gates import GateReport, evaluate_gates
from proofstep_types import (
    CalibrationRequirementSpec,
    ExampleResult,
    GateRule,
    GateSet,
    Metric,
    ResultStatus,
    Score,
    Severity,
)


def slice_key(slice_: dict[str, Any] | None) -> str:
    """Canonical rendering, so a unique index over a JSONB slice is simple.

    A `UNIQUE (run_id, metric_key, slice)` on JSONB would compare `{"a":1,"b":2}`
    and `{"b":2,"a":1}` as different rows for the same logical slice.
    """
    if not slice_:
        return ""
    return ",".join(f"{k}={slice_[k]}" for k in sorted(slice_))


@dataclass(slots=True)
class RunTotals:
    completed: int = 0
    failed: int = 0
    cost: Decimal = field(default_factory=lambda: Decimal(0))


class ExperimentService:
    def __init__(self, session: AsyncSession, *, project_id: uuid.UUID) -> None:
        self.session = session
        self.project_id = project_id

    # ------------------------------------------------------------------ lookups

    async def get_run(self, run_id: uuid.UUID) -> ExperimentRun:
        run = await self.session.get(ExperimentRun, run_id)
        if run is None or run.project_id != self.project_id:
            raise NotFoundError("No such experiment run.")
        return run

    async def get_experiment(self, experiment_id: uuid.UUID) -> Experiment:
        experiment = await self.session.get(Experiment, experiment_id)
        if experiment is None or experiment.project_id != self.project_id:
            raise NotFoundError("No such experiment.")
        return experiment

    # -------------------------------------------------------------------- runs

    async def open_run(self, experiment_id: uuid.UUID, *, trigger: str = "cli") -> ExperimentRun:
        experiment = await self.get_experiment(experiment_id)
        previous = (
            await self.session.execute(
                select(ExperimentRun.attempt)
                .where(ExperimentRun.experiment_id == experiment.id)
                .order_by(ExperimentRun.attempt.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        run = ExperimentRun(
            project_id=self.project_id,
            experiment_id=experiment.id,
            attempt=(previous or 0) + 1,
            status="running",
            trigger=trigger,
            started_at=datetime.now(UTC),
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def append_results(
        self, run_id: uuid.UUID, results: list[ExampleResult]
    ) -> tuple[int, int]:
        """Store per-example results and their scores. Append-only.

        Returns (stored, skipped). Skipping a duplicate rather than erroring keeps a
        resumed or retried upload safe: the CLI streams results in chunks, and a
        chunk that half-succeeded must be re-sendable.
        """
        run = await self.get_run(run_id)
        if run.status in ("succeeded", "cancelled"):
            raise ConflictError(
                f"Run is already {run.status}; results cannot be added to a finished run."
            )

        existing = set(
            (
                await self.session.execute(
                    select(ExperimentResult.external_id).where(ExperimentResult.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )

        stored = skipped = 0
        for result in results:
            if result.example_id in existing:
                skipped += 1
                continue
            existing.add(result.example_id)

            row = ExperimentResult(
                project_id=self.project_id,
                run_id=run_id,
                external_id=result.example_id,
                status=result.status.value,
                output=_jsonable(result.output),
                trace_id=result.trace.trace_id if result.trace else None,
                latency_ms=result.latency_ms,
                tokens=result.tokens,
                cost=result.cost,
                retry_count=result.retry_count,
                error=result.error.message if result.error else None,
            )
            self.session.add(row)
            await self.session.flush()

            for score in result.scores:
                self.session.add(
                    EvaluationResult(
                        project_id=self.project_id,
                        experiment_result_id=row.id,
                        metric_key=score.metric,
                        score=score.value,
                        passed=score.passed,
                        label=score.label,
                        value_json=_jsonable(score.raw),
                        slice=score.slice,
                        reasoning=score.reasoning,
                        confidence=score.confidence,
                        error=score.error,
                        cost=score.cost,
                        latency_ms=score.latency_ms,
                    )
                )
            stored += 1

        await self.session.flush()
        await self._refresh_totals(run)
        return stored, skipped

    async def _refresh_totals(self, run: ExperimentRun) -> None:
        rows = (
            (
                await self.session.execute(
                    select(ExperimentResult).where(ExperimentResult.run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        run.completed_examples = len(rows)
        run.failed_examples = sum(1 for r in rows if r.status != "ok")
        run.total_cost = sum((r.cost for r in rows), Decimal(0))

    async def complete_run(
        self, run_id: uuid.UUID, *, status: str = "succeeded", error: str | None = None
    ) -> ExperimentRun:
        run = await self.get_run(run_id)
        run.status = status
        run.error = error
        run.ended_at = datetime.now(UTC)
        await self._recompute_aggregates(run)
        await self.session.flush()
        return run

    async def cancel_run(self, run_id: uuid.UUID) -> ExperimentRun:
        run = await self.get_run(run_id)
        if run.status in ("succeeded", "failed"):
            raise ConflictError(f"Run already finished with status {run.status!r}.")
        run.status = "cancelled"
        run.cancelled_at = datetime.now(UTC)
        run.ended_at = run.cancelled_at
        # Aggregate what completed. A cancelled run still carries information, and
        # discarding it would waste whatever it cost to produce.
        await self._recompute_aggregates(run)
        await self.session.flush()
        return run

    # ------------------------------------------------------------- aggregation

    async def _recompute_aggregates(self, run: ExperimentRun) -> None:
        """Roll scores up through `evaluation-core`, not through SQL.

        Doing this in SQL would be faster and would also be a second implementation
        of the aggregation rules — including the one that excludes errored
        evaluations from the mean. Two implementations of that rule is one too many.
        """
        results = await self.load_results(run.id)
        metrics = aggregate_scores(results, confidence_intervals=True)

        await self.session.execute(delete(AggregateMetric).where(AggregateMetric.run_id == run.id))
        for metric in metrics:
            self.session.add(
                AggregateMetric(
                    project_id=self.project_id,
                    run_id=run.id,
                    metric_key=metric.key,
                    slice_key=slice_key(metric.slice),
                    slice=metric.slice,
                    value=metric.value,
                    count=metric.count,
                    error_count=metric.error_count,
                    stddev=metric.stddev,
                    ci_low=metric.ci_low,
                    ci_high=metric.ci_high,
                    unit=metric.unit,
                )
            )
        await self.session.flush()

    async def submit_metrics(
        self, run_id: uuid.UUID, metrics: list[Metric]
    ) -> tuple[int, list[str]]:
        """Store metrics the server cannot compute for itself, and refuse the ones it can.

        The boundary, and why it is drawn exactly here: metrics derived from per-example scores —
        accuracy, a judge's mean rating — are recomputed server-side from the stored scores, and
        that recomputation is what makes the server's verdict *verified* rather than merely
        reported. Accepting a client's number for one of those would hollow it out: a run could
        claim any accuracy it liked and the dashboard would agree.

        Corpus and operational metrics are different in kind. NDCG over a whole run, a confusion
        matrix, p95 latency — none can be reconstructed from individual scores, because they are
        properties of the set rather than sums over it. Only the process that ran the suite has
        them, so either the client submits them or every gate on them evaluates as "metric
        missing" — which is exactly what happened the first time a suite with a protected-class
        gate was published: the server read ERROR on a run the CLI had passed.

        So: submitted metrics are stored only for keys the server did not compute. Collisions are
        returned rather than silently dropped, because a client that thinks it is setting a number
        and is not deserves to hear about it.
        """
        run = await self.get_run(run_id)
        computed = {
            (row.metric_key, row.slice_key)
            for row in (
                await self.session.execute(
                    select(AggregateMetric).where(AggregateMetric.run_id == run.id)
                )
            )
            .scalars()
            .all()
        }

        stored = 0
        rejected: list[str] = []
        for metric in metrics:
            key = (metric.key, slice_key(metric.slice))
            if key in computed:
                rejected.append(metric.full_key)
                continue
            self.session.add(
                AggregateMetric(
                    project_id=self.project_id,
                    run_id=run.id,
                    metric_key=metric.key,
                    slice_key=key[1],
                    slice=metric.slice,
                    value=metric.value,
                    count=metric.count,
                    error_count=metric.error_count,
                    stddev=metric.stddev,
                    ci_low=metric.ci_low,
                    ci_high=metric.ci_high,
                    unit=metric.unit,
                )
            )
            # Added to the set as we go, so a duplicate key inside one submission is rejected too
            # rather than producing two rows the reader cannot tell apart.
            computed.add(key)
            stored += 1

        await self.session.flush()
        return stored, rejected

    async def load_results(self, run_id: uuid.UUID) -> list[ExampleResult]:
        """Rehydrate stored rows into the core's own result type.

        Reconstituting the domain object is what lets the server call the same
        aggregation and gate code the CLI does.
        """
        rows = (
            (
                await self.session.execute(
                    select(ExperimentResult)
                    .where(ExperimentResult.run_id == run_id)
                    .order_by(ExperimentResult.external_id)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return []

        scores_by_result: dict[uuid.UUID, list[Score]] = {}
        evaluations = (
            (
                await self.session.execute(
                    select(EvaluationResult).where(
                        EvaluationResult.experiment_result_id.in_([r.id for r in rows])
                    )
                )
            )
            .scalars()
            .all()
        )
        for evaluation in evaluations:
            assert evaluation.experiment_result_id is not None
            scores_by_result.setdefault(evaluation.experiment_result_id, []).append(
                Score(
                    metric=evaluation.metric_key,
                    value=evaluation.score,
                    passed=evaluation.passed,
                    label=evaluation.label,
                    raw=evaluation.value_json,
                    slice=evaluation.slice,
                    reasoning=evaluation.reasoning,
                    confidence=evaluation.confidence,
                    error=evaluation.error,
                    cost=evaluation.cost,
                    latency_ms=evaluation.latency_ms,
                )
            )

        return [
            ExampleResult(
                example_id=row.external_id,
                status=ResultStatus(row.status),
                output=row.output,
                scores=scores_by_result.get(row.id, []),
                latency_ms=row.latency_ms,
                tokens=row.tokens,
                cost=row.cost,
                retry_count=row.retry_count,
            )
            for row in rows
        ]

    async def load_metrics(self, run_id: uuid.UUID) -> list[Metric]:
        rows = (
            (
                await self.session.execute(
                    select(AggregateMetric).where(
                        AggregateMetric.project_id == self.project_id,
                        AggregateMetric.run_id == run_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            Metric(
                key=row.metric_key,
                value=row.value,
                count=row.count,
                error_count=row.error_count,
                stddev=row.stddev,
                ci_low=row.ci_low,
                ci_high=row.ci_high,
                unit=row.unit,
                slice=row.slice,
            )
            for row in rows
        ]

    # --------------------------------------------------------------- comparison

    async def compare(
        self, candidate_run_id: uuid.UUID, baseline_run_id: uuid.UUID | None
    ) -> tuple[Comparison, bool]:
        candidate = await self.get_run(candidate_run_id)
        candidate_metrics = await self.load_metrics(candidate.id)
        candidate_experiment = await self.get_experiment(candidate.experiment_id)

        if baseline_run_id is None:
            return compare_metrics(candidate_metrics, []), True

        baseline = await self.get_run(baseline_run_id)
        baseline_metrics = await self.load_metrics(baseline.id)
        baseline_experiment = await self.get_experiment(baseline.experiment_id)

        candidate_hash = (candidate_experiment.dataset_content_hash or b"").hex()
        baseline_hash = (baseline_experiment.dataset_content_hash or b"").hex()

        comparison = compare_metrics(
            candidate_metrics,
            baseline_metrics,
            candidate_results=await self.load_results(candidate.id),
            baseline_results=await self.load_results(baseline.id),
            candidate_hash=candidate_hash,
            baseline_hash=baseline_hash,
        )
        return comparison, comparison.dataset_match

    async def resolve_baseline(
        self, *, suite_name: str, branch: str = "main", exclude_run_id: uuid.UUID | None = None
    ) -> ExperimentRun | None:
        """Latest successful run for this suite on the baseline branch (ADR-013).

        Matches how engineers think about regression — "did my branch make it worse
        than main?" — and needs no curation to work on day one. A promoted baseline
        takes precedence when one exists.
        """
        promoted = (
            await self.session.execute(
                select(ExperimentRun)
                .join(Experiment, Experiment.id == ExperimentRun.experiment_id)
                .where(
                    Experiment.project_id == self.project_id,
                    Experiment.suite_name == suite_name,
                    Experiment.is_baseline.is_(True),
                    ExperimentRun.status.in_(("succeeded", "partial")),
                )
                .order_by(ExperimentRun.ended_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if promoted is not None:
            return promoted

        statement = (
            select(ExperimentRun)
            .join(Experiment, Experiment.id == ExperimentRun.experiment_id)
            .where(
                Experiment.project_id == self.project_id,
                Experiment.suite_name == suite_name,
                Experiment.git_branch == branch,
                ExperimentRun.status.in_(("succeeded", "partial")),
            )
            .order_by(ExperimentRun.ended_at.desc())
            .limit(1)
        )
        if exclude_run_id is not None:
            statement = statement.where(ExperimentRun.id != exclude_run_id)
        return (await self.session.execute(statement)).scalar_one_or_none()

    # -------------------------------------------------------------------- gates

    async def load_gate_set(self, gate_set_id: uuid.UUID) -> GateSet:
        gate_set = await self.session.get(QualityGateSet, gate_set_id)
        if gate_set is None or gate_set.project_id != self.project_id:
            raise NotFoundError("No such gate set.")

        rules = (
            (
                await self.session.execute(
                    select(QualityGateRule).where(QualityGateRule.gate_set_id == gate_set.id)
                )
            )
            .scalars()
            .all()
        )
        return GateSet(
            name=gate_set.name,
            require_dataset_match=gate_set.require_dataset_match,
            # Two columns, one field: the boolean says whether calibration is enforced
            # and the JSONB says with which thresholds. Rebuilding the spec when the
            # JSONB is present keeps a tightened threshold visible on the server side
            # rather than collapsing back to the defaults.
            require_calibration=(
                CalibrationRequirementSpec(**gate_set.calibration_requirement)
                if gate_set.calibration_requirement
                else gate_set.require_calibration
            ),
            rules=[
                GateRule(
                    metric_key=rule.metric_key,
                    minimum=rule.minimum,
                    maximum=rule.maximum,
                    max_absolute_regression=rule.max_absolute_regression,
                    max_relative_regression=rule.max_relative_regression,
                    severity=Severity(rule.severity),
                    slice=rule.slice,
                    require_baseline=rule.require_baseline,
                    max_error_rate=rule.max_error_rate,
                    significance=rule.significance,
                    require_power=rule.require_power,
                )
                for rule in rules
            ],
        )

    async def evaluate_gates(
        self,
        *,
        gate_set_id: uuid.UUID,
        candidate_run_id: uuid.UUID,
        baseline_run_id: uuid.UUID | None,
        dataset_match: bool = True,
    ) -> GateReport:
        """Delegates to `evaluation-core`, so the verdict cannot drift from the CLI."""
        gate_set = await self.load_gate_set(gate_set_id)
        candidate = await self.load_metrics(candidate_run_id)
        baseline = await self.load_metrics(baseline_run_id) if baseline_run_id else None
        return evaluate_gates(gate_set, candidate, baseline, dataset_match=dataset_match)


def _jsonable(value: Any) -> Any:
    """Coerce arbitrary task output into something JSONB accepts."""
    if value is None or isinstance(value, str | int | float | bool | dict | list):
        return value
    return str(value)
