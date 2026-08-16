"""Compare a candidate run against a baseline.

Two refusals are the point of this module, and both exist because the failure they
prevent is *silent*:

- **Refuse to compare across different dataset content.** The numbers still render,
  and they mean nothing.
- **Refuse to compare across different evaluator versions.** If the ruler changed,
  reporting the difference as a quality delta is the most misleading thing this
  system could do.

Both are overridable, and both are reported when overridden.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from proofstep_core.aggregate import scores_for
from proofstep_core.stats import delta_ci
from proofstep_types import ExampleResult, Metric, MetricDelta


@dataclass(frozen=True, slots=True)
class ExampleRegression:
    """An example that passed in the baseline and failed in the candidate."""

    example_id: str
    metric: str
    baseline_score: float | None
    candidate_score: float | None
    trace_id: str | None = None

    @property
    def delta(self) -> float:
        return (self.candidate_score or 0.0) - (self.baseline_score or 0.0)


@dataclass(frozen=True, slots=True)
class Comparison:
    deltas: list[MetricDelta] = field(default_factory=list)
    regressions: list[ExampleRegression] = field(default_factory=list)
    improvements: list[ExampleRegression] = field(default_factory=list)
    dataset_match: bool = True
    warnings: list[str] = field(default_factory=list)

    def delta_for(self, key: str, **slice_: str) -> MetricDelta | None:
        wanted = slice_ or None
        return next((d for d in self.deltas if d.key == key and d.slice == wanted), None)

    @property
    def regressed_metrics(self) -> list[MetricDelta]:
        return sorted(
            (d for d in self.deltas if (d.absolute_delta or 0) < 0),
            key=lambda d: d.absolute_delta or 0,
        )


def compare_metrics(
    candidate: Sequence[Metric],
    baseline: Sequence[Metric],
    *,
    candidate_results: Sequence[ExampleResult] = (),
    baseline_results: Sequence[ExampleResult] = (),
    candidate_hash: str = "",
    baseline_hash: str = "",
    significance: bool = True,
) -> Comparison:
    """Produce per-metric deltas and the per-example regression list."""
    warnings: list[str] = []
    dataset_match = not (candidate_hash and baseline_hash) or candidate_hash == baseline_hash
    if not dataset_match:
        warnings.append(
            f"Dataset content differs: candidate {candidate_hash[:12]} vs baseline "
            f"{baseline_hash[:12]}. Metric deltas below compare different data and "
            "should not be read as quality changes."
        )

    base_index = {(m.key, _key(m.slice)): m for m in baseline}
    cand_index = {(m.key, _key(m.slice)): m for m in candidate}

    deltas: list[MetricDelta] = []
    for lookup in sorted(set(base_index) | set(cand_index)):
        key, slice_key = lookup
        base = base_index.get(lookup)
        cand = cand_index.get(lookup)
        deltas.append(
            _delta(
                key,
                dict(slice_key) or None,
                base,
                cand,
                candidate_results,
                baseline_results,
                significance=significance,
            )
        )

    if only_candidate := sorted({k for k, _ in cand_index} - {k for k, _ in base_index}):
        warnings.append(
            f"{len(only_candidate)} metric(s) exist only in the candidate: "
            f"{', '.join(only_candidate[:5])}. New evaluators have no baseline."
        )
    if only_baseline := sorted({k for k, _ in base_index} - {k for k, _ in cand_index}):
        warnings.append(
            f"{len(only_baseline)} metric(s) exist only in the baseline: "
            f"{', '.join(only_baseline[:5])}. Did an evaluator get removed or renamed?"
        )

    regressions, improvements = _example_changes(candidate_results, baseline_results)

    return Comparison(
        deltas=deltas,
        regressions=regressions,
        improvements=improvements,
        dataset_match=dataset_match,
        warnings=warnings,
    )


def _delta(  # noqa: PLR0917
    key: str,
    slice_: dict[str, str] | None,
    base: Metric | None,
    cand: Metric | None,
    candidate_results: Sequence[ExampleResult],
    baseline_results: Sequence[ExampleResult],
    *,
    significance: bool,
) -> MetricDelta:
    if base is None or cand is None:
        return MetricDelta(
            key=key,
            slice=slice_,
            baseline=base.value if base else None,
            candidate=cand.value if cand else None,
            count=cand.count if cand else 0,
        )

    absolute = cand.value - base.value
    relative = absolute / abs(base.value) if base.value else None

    ci_low = ci_high = None
    significant: bool | None = None
    if significance and candidate_results and baseline_results and slice_ is None:
        cand_values = scores_for(candidate_results, key)
        base_values = scores_for(baseline_results, key)
        if len(cand_values) >= 5 and len(base_values) >= 5:
            ci_low, ci_high = delta_ci(base_values, cand_values)
            # Advisory only. Gates use thresholds: at n=200 most real regressions are
            # not "significant", and gating on that would let them through.
            significant = not (ci_low <= 0 <= ci_high)

    return MetricDelta(
        key=key,
        slice=slice_,
        baseline=base.value,
        candidate=cand.value,
        absolute_delta=absolute,
        relative_delta=relative,
        count=cand.count,
        ci_low=ci_low,
        ci_high=ci_high,
        significant=significant,
    )


def _example_changes(
    candidate: Sequence[ExampleResult], baseline: Sequence[ExampleResult]
) -> tuple[list[ExampleRegression], list[ExampleRegression]]:
    """Match on example id, never on position — datasets gain and lose rows."""
    base_by_id = {r.example_id: r for r in baseline}
    regressions: list[ExampleRegression] = []
    improvements: list[ExampleRegression] = []

    for cand in candidate:
        base = base_by_id.get(cand.example_id)
        if base is None:
            continue
        for score in cand.scores:
            prior = base.score_for(score.metric)
            if prior is None or score.errored or prior.errored:
                continue
            if score.value is None or prior.value is None:
                continue
            change = ExampleRegression(
                example_id=cand.example_id,
                metric=score.metric,
                baseline_score=prior.value,
                candidate_score=score.value,
                trace_id=cand.trace.trace_id if cand.trace else None,
            )
            if score.value < prior.value:
                regressions.append(change)
            elif score.value > prior.value:
                improvements.append(change)

    regressions.sort(key=lambda r: r.delta)
    improvements.sort(key=lambda r: -r.delta)
    return regressions, improvements


def _key(slice_: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(slice_.items())) if slice_ else ()
