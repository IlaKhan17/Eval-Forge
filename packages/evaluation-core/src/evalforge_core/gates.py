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

from dataclasses import dataclass, field

from evalforge_types import ExitCode, GateResult, GateRule, GateSet, Metric, Severity, Verdict


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
) -> GateReport:
    """Apply every rule in the set and combine the verdicts."""
    index = {(m.key, _key(m.slice)): m for m in candidate}
    base_index = {(m.key, _key(m.slice)): m for m in (baseline or [])}

    results = [_apply(rule, index, base_index) for rule in gate_set.rules]

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


def _apply(  # noqa: PLR0911 — one early return per gate clause reads better than nesting
    rule: GateRule,
    candidate: dict[tuple[str, tuple[tuple[str, str], ...]], Metric],
    baseline: dict[tuple[str, tuple[tuple[str, str], ...]], Metric],
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

        if regression := _check_regression(rule, metric, base):
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


def _check_regression(rule: GateRule, metric: Metric, base: Metric) -> GateResult | None:
    drop = base.value - metric.value

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


def _key(slice_: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(slice_.items())) if slice_ else ()
