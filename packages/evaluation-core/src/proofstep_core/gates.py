"""The quality gate engine — the component that turns metrics into a merge decision.

Two properties matter more than anything else here:

1. **A metric a gate names but cannot find is an ERROR, not a pass.** A typo'd metric
   key that silently passes is worse than no gate at all, because it produces false
   assurance that nobody will ever check.

2. **Protected metrics use absolute floors, not regression thresholds.** Aggregate
   averages hide rare-class collapse: a 3%-prevalence class going from 0.99 to 0.20
   recall moves macro accuracy by ~0.3%, passing any `max_regression: 0.02` gate
   while the system silently ignores unsubscribe requests. Only a sliced, absolute,
   blocking floor catches that (docs/EVALUATION_ENGINE.md §7).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from proofstep_core.significance import SignificanceResult
from proofstep_types import (
    CalibrationStatus,
    ExitCode,
    GateResult,
    GateRule,
    GateSet,
    Metric,
    Severity,
    Verdict,
)


@dataclass(frozen=True, slots=True)
class GateReport:
    """The full verdict of a gate set against a candidate run."""

    results: list[GateResult] = field(default_factory=list)
    verdict: Verdict = Verdict.PASS

    @property
    def passed(self) -> bool:
        return not self.verdict.is_blocking

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [r for r in self.results if r.verdict in ("fail", "error") and r.blocking]

    @property
    def warnings(self) -> list[GateResult]:
        return [r for r in self.results if r.verdict in ("fail", "error") and not r.blocking]

    @property
    def exit_code(self) -> int:
        if self.verdict is Verdict.ERROR:
            return ExitCode.EXECUTION_ERROR
        if self.verdict is Verdict.FAIL:
            return ExitCode.BLOCKING_FAILURE
        return ExitCode.PASS


def evaluate_gates(
    gate_set: GateSet,
    candidate: list[Metric],
    baseline: list[Metric] | None = None,
    *,
    dataset_match: bool = True,
    judge_metrics: Collection[str] = (),
    calibrations: Mapping[str, CalibrationStatus] | None = None,
    significance: Mapping[str, SignificanceResult] | None = None,
) -> GateReport:
    """Apply every rule in the set and combine the verdicts.

    `judge_metrics` names the metrics produced by LLM judges. The gate engine cannot
    infer it — a `Metric` is just a key and a number — and it is needed because gating on
    a judge nobody has checked is the specific thing calibration exists to catch.

    `significance` carries the paired tests, keyed by metric. Computed by the caller rather than
    here, because the test needs *per-example* results and this function deliberately takes only
    aggregates — that boundary is what lets the same gate engine run against a report loaded from
    disk. A rule that asked for a test but got no result is treated as untested, never as passed.
    """
    index = {(m.key, _key(m.slice)): m for m in candidate}
    base_index = {(m.key, _key(m.slice)): m for m in (baseline or [])}

    results = [_apply(rule, index, base_index, significance or {}) for rule in gate_set.rules]
    results.extend(
        _calibration_results(gate_set, judge_metrics=judge_metrics, calibrations=calibrations or {})
    )

    if gate_set.require_dataset_match and not dataset_match:
        results.append(
            GateResult(
                metric_key="__dataset_match__",
                verdict=Verdict.ERROR.value,
                severity=Severity.BLOCK,
                rule="require_dataset_match",
                message=(
                    "Candidate and baseline ran against different dataset content. "
                    "The comparison is not meaningful; re-run the baseline against "
                    "the same locked version, or set require_dataset_match: false."
                ),
            )
        )

    return GateReport(results=results, verdict=_combine(results))


def _calibration_results(
    gate_set: GateSet,
    *,
    judge_metrics: Collection[str],
    calibrations: Mapping[str, CalibrationStatus],
) -> list[GateResult]:
    """One result per gated judge metric, describing the state of its calibration.

    Four distinct states, because they have four different fixes:

    - **no calibration** — go and calibrate it
    - **calibration for a different evaluator version** — the rubric or model changed, so
      the old evidence does not apply; re-calibrate
    - **calibrated and failing the requirement** — fix the judge or the rubric
    - **calibrated and satisfying it** — nothing to do, but the numbers still appear in
      the report so the reader can see what "trusted" rests on

    Whether these block depends on `require_calibration`. When it is off they are
    warnings, never silence: a merge gated on an unvalidated number is worth saying out
    loud even when nobody has asked for it to be enforced.
    """
    requirement = gate_set.calibration_requirement
    gated_judges = sorted(
        {rule.metric_key for rule in gate_set.rules if rule.metric_key in set(judge_metrics)}
    )
    if not gated_judges:
        return []

    blocking = requirement is not None and requirement.required
    severity = Severity.BLOCK if blocking else Severity.WARN
    results: list[GateResult] = []

    for metric_key in gated_judges:
        status = calibrations.get(metric_key)

        if status is None or not status.calibrated:
            results.append(
                GateResult(
                    metric_key=metric_key,
                    verdict=(Verdict.ERROR if blocking else Verdict.FAIL).value,
                    severity=severity,
                    rule="uncalibrated_judge",
                    message=(
                        f"{metric_key} is gated on an LLM judge with no calibration. "
                        "The number has never been checked against a human, so the "
                        "gate is blocking merges on an unvalidated measurement. Run "
                        "`proofstep calibrate` against a labelled set."
                    ),
                )
            )
            continue

        if status.is_stale:
            results.append(
                GateResult(
                    metric_key=metric_key,
                    verdict=(Verdict.ERROR if blocking else Verdict.FAIL).value,
                    severity=severity,
                    rule="stale_calibration",
                    message=(
                        f"{metric_key} has a calibration for evaluator version "
                        f"{status.evaluator_version_hash} but the run used "
                        f"{status.stale_for_version}. The rubric, model, or judge "
                        "parameters changed, which means the ruler changed; the old "
                        "calibration says nothing about the new judge."
                    ),
                )
            )
            continue

        if status.satisfied is False:
            results.append(
                GateResult(
                    metric_key=metric_key,
                    verdict=(Verdict.ERROR if blocking else Verdict.FAIL).value,
                    severity=severity,
                    rule="calibration_requirement",
                    actual=status.kappa,
                    message=(
                        f"{metric_key} judge calibration does not meet the requirement: "
                        + "; ".join(status.failures)
                    ),
                )
            )
            continue

        results.append(
            GateResult(
                metric_key=metric_key,
                verdict=Verdict.PASS.value,
                severity=Severity.WARN,
                rule="calibrated",
                actual=status.kappa,
                message=_calibrated_message(status),
            )
        )

    return results


def _calibrated_message(status: CalibrationStatus) -> str:
    kappa = "undefined" if status.kappa is None else f"{status.kappa:.3f}"
    agreement = "?" if status.agreement is None else f"{status.agreement:.3f}"
    ceiling = " (at the human ceiling)" if status.at_human_ceiling else ""
    suffix = f" — {'; '.join(status.warnings)}" if status.warnings else ""
    return (
        f"{status.metric_key} judge calibrated on {status.n_examples} examples: "
        f"agreement {agreement}, κ {kappa}{ceiling}{suffix}"
    )


def _apply(  # noqa: PLR0911 — one early return per gate clause reads better than nesting
    rule: GateRule,
    candidate: dict[tuple[str, tuple[tuple[str, str], ...]], Metric],
    baseline: dict[tuple[str, tuple[tuple[str, str], ...]], Metric],
    significance: Mapping[str, SignificanceResult],
) -> GateResult:
    lookup = (rule.metric_key, _key(rule.slice))
    metric = candidate.get(lookup)

    if metric is None:
        available = ", ".join(sorted({k for k, _ in candidate})[:10]) or "<none>"
        return _result(
            rule,
            Verdict.ERROR,
            "metric_missing",
            message=(
                f"No metric named {rule.full_key!r} was produced. Available metrics: "
                f"{available}. A gate on a metric that does not exist cannot pass."
            ),
        )

    if metric.count == 0:
        return _result(
            rule,
            Verdict.ERROR,
            "no_data",
            actual=None,
            message=(
                f"{rule.full_key} has no successful evaluations "
                f"({metric.error_count} errored). Nothing was measured."
            ),
        )

    if metric.error_rate > rule.max_error_rate:
        return _result(
            rule,
            Verdict.ERROR,
            "error_rate",
            actual=metric.value,
            threshold=rule.max_error_rate,
            message=(
                f"{rule.full_key}: {metric.error_count} of "
                f"{metric.count + metric.error_count} evaluations errored "
                f"({metric.error_rate:.1%} > {rule.max_error_rate:.1%}). The score is "
                "not trustworthy enough to gate on."
            ),
        )

    # Absolute thresholds first: they do not depend on a baseline, which is exactly
    # why protected metrics use them. A bad merge that becomes the baseline cannot
    # weaken an absolute floor.
    if rule.minimum is not None and metric.value < rule.minimum:
        return _result(
            rule,
            Verdict.FAIL,
            "minimum",
            actual=metric.value,
            threshold=rule.minimum,
            message=f"{rule.full_key} {metric.value:.4g} < minimum {rule.minimum:.4g}",
        )

    if rule.maximum is not None and metric.value > rule.maximum:
        return _result(
            rule,
            Verdict.FAIL,
            "maximum",
            actual=metric.value,
            threshold=rule.maximum,
            message=f"{rule.full_key} {metric.value:.4g} > maximum {rule.maximum:.4g}",
        )

    if rule.needs_baseline or rule.require_baseline:
        base = baseline.get(lookup)
        if base is None:
            if rule.require_baseline:
                return _result(
                    rule,
                    Verdict.ERROR,
                    "baseline_missing",
                    actual=metric.value,
                    message=(
                        f"{rule.full_key} requires a baseline but none was found. "
                        "Run the suite on the baseline branch first, or promote an "
                        "experiment to be the baseline."
                    ),
                )
            # No baseline and none required: absolute checks already passed, and a
            # regression check against nothing is vacuous rather than failing.
            return _result(
                rule,
                Verdict.PASS,
                "no_baseline",
                actual=metric.value,
                message=f"{rule.full_key} {metric.value:.4g} (no baseline to compare against)",
            )

        if regression := _check_regression(rule, metric, base, significance.get(rule.metric_key)):
            return regression

        return _result(
            rule,
            Verdict.PASS,
            "regression",
            actual=metric.value,
            baseline=base.value,
            message=(
                f"{rule.full_key} {metric.value:.4g} vs baseline {base.value:.4g} "
                f"({metric.value - base.value:+.4g})"
            ),
        )

    return _result(
        rule,
        Verdict.PASS,
        "threshold",
        actual=metric.value,
        message=f"{rule.full_key} {metric.value:.4g}",
    )


def _check_regression(
    rule: GateRule,
    metric: Metric,
    base: Metric,
    test: SignificanceResult | None = None,
) -> GateResult | None:
    drop = base.value - metric.value

    if power := _check_power(rule, metric, base, test):
        return power

    if noise := _explained_by_noise(rule, metric, base, test, drop):
        return noise

    if rule.max_absolute_regression is not None and drop > rule.max_absolute_regression:
        return _result(
            rule,
            Verdict.FAIL,
            "max_absolute_regression",
            actual=metric.value,
            baseline=base.value,
            threshold=rule.max_absolute_regression,
            message=(
                f"{rule.full_key} regressed {drop:.4g} from baseline {base.value:.4g} "
                f"(allowed {rule.max_absolute_regression:.4g})"
            ),
        )

    if rule.max_relative_regression is not None and base.value != 0:
        relative = drop / abs(base.value)
        if relative > rule.max_relative_regression:
            return _result(
                rule,
                Verdict.FAIL,
                "max_relative_regression",
                actual=metric.value,
                baseline=base.value,
                threshold=rule.max_relative_regression,
                message=(
                    f"{rule.full_key} regressed {relative:.1%} from baseline "
                    f"{base.value:.4g} (allowed {rule.max_relative_regression:.1%})"
                ),
            )
    return None


def _result(
    rule: GateRule,
    verdict: Verdict,
    clause: str,
    *,
    actual: float | None = None,
    baseline: float | None = None,
    threshold: float | None = None,
    message: str = "",
) -> GateResult:
    return GateResult(
        metric_key=rule.metric_key,
        slice=rule.slice,
        verdict=verdict.value,
        severity=rule.severity,
        rule=clause,
        threshold=threshold,
        actual=actual,
        baseline=baseline,
        message=message,
    )


def _combine(results: list[GateResult]) -> Verdict:
    """Worst verdict wins, but a non-blocking failure only ever produces WARN.

    Severity is what separates "this is bad and you may not merge" from "this is bad
    and you should know". Collapsing them would either block on noise or hide real
    failures, and both destroy trust in the gate.
    """
    verdict = Verdict.PASS
    for result in results:
        if result.verdict not in ("fail", "error"):
            continue
        if not result.blocking:
            verdict = max(verdict, Verdict.WARN, key=_rank)
        elif result.verdict == "error":
            verdict = max(verdict, Verdict.ERROR, key=_rank)
        else:
            verdict = max(verdict, Verdict.FAIL, key=_rank)
    return verdict


_RANK = {Verdict.PASS: 0, Verdict.WARN: 1, Verdict.FAIL: 2, Verdict.ERROR: 3}


def _rank(v: Verdict) -> int:
    return _RANK[v]


def _check_power(
    rule: GateRule,
    metric: Metric,
    base: Metric,
    test: SignificanceResult | None,
) -> GateResult | None:
    """Refuse to certify a gate the run could never have failed.

    `max_absolute_regression: 0.02` over twenty noisy examples is a promise the data cannot keep.
    The rule looks satisfied, the build goes green, and nobody learns that the check was incapable
    of detecting the thing it names. Opt-in via `require_power`, because on a small suite the honest
    answer is often "add examples", and forcing that on every existing gate would be a breaking
    change dressed up as rigour.
    """
    if not rule.require_power:
        return None

    threshold = rule.max_absolute_regression
    if threshold is None:
        return None

    if test is None or test.minimum_detectable_effect is None:
        return _result(
            rule,
            Verdict.ERROR,
            "power_unknown",
            actual=metric.value,
            baseline=base.value,
            threshold=threshold,
            message=(
                f"{rule.full_key} requires a powered comparison, but no paired test was available "
                "(too few examples ran on both sides, or the runs share no examples)."
            ),
        )

    if test.underpowered_for(threshold):
        return _result(
            rule,
            Verdict.ERROR,
            "underpowered",
            actual=metric.value,
            baseline=base.value,
            threshold=threshold,
            message=(
                f"{rule.full_key} gates on a {threshold:.4g} regression, but {test.n_pairs} paired "
                f"example(s) could only detect {test.minimum_detectable_effect:.4g}. This gate "
                "cannot see what it claims to guard — add examples or widen the threshold."
            ),
        )
    return None


def _explained_by_noise(
    rule: GateRule,
    metric: Metric,
    base: Metric,
    test: SignificanceResult | None,
    drop: float,
) -> GateResult | None:
    """Pass a threshold-breaking regression that the data cannot distinguish from noise.

    Only when the rule opted in with `significance`. This is the half a threshold cannot do: at
    forty examples a two-point drop is one example flipping, and blocking a merge on it teaches
    people to bypass the gate that also carries the real checks.

    Reported as PASS with the numbers attached rather than silently — "we measured a drop and could
    not tell it from noise" is a different statement from "nothing moved", and a reader deciding
    whether to trust the green tick needs the difference.
    """
    if rule.significance is None:
        return None
    if rule.max_absolute_regression is None or drop <= rule.max_absolute_regression:
        return None

    if test is None:
        # Asked for a test and got none. Untested is not passed: a rule that quietly downgrades to
        # a plain threshold when the data is missing is a rule nobody can reason about.
        return _result(
            rule,
            Verdict.ERROR,
            "significance_unavailable",
            actual=metric.value,
            baseline=base.value,
            message=(
                f"{rule.full_key} asks for a significance test, but no paired comparison was "
                "available. The two runs must share examples for this rule to be decidable."
            ),
        )

    if test.is_significant(rule.significance):
        return None

    p_value = test.adjusted_p_value if test.adjusted_p_value is not None else test.p_value
    return _result(
        rule,
        Verdict.PASS,
        "not_significant",
        actual=metric.value,
        baseline=base.value,
        threshold=rule.max_absolute_regression,
        message=(
            f"{rule.full_key} measured a {drop:.4g} regression over {test.n_pairs} paired "
            f"example(s), which this run cannot distinguish from noise "
            f"(p={p_value:.3g} > {rule.significance:g}). Not failing the build on it."
        ),
    )


def _key(slice_: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(slice_.items())) if slice_ else ()
