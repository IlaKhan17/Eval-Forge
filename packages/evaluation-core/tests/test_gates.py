"""Gate engine tests.

The gate engine decides whether a pull request merges, so its failure modes matter
more than its happy path. The tests below are weighted accordingly.
"""

from __future__ import annotations

from typing import Any

import pytest

from evalforge_core.gates import evaluate_gates
from evalforge_types import GateRule, GateSet, Metric, Severity, Verdict


def metric(
    key: str,
    value: float,
    *,
    n: int = 100,
    errors: int = 0,
    slice: dict[str, str] | None = None,
) -> Metric:
    return Metric(key=key, value=value, count=n, error_count=errors, slice=slice)


def gate(**kw: Any) -> GateSet:
    return GateSet(rules=[GateRule(**kw)])


class TestAbsoluteThresholds:
    async def test_minimum_passes(self) -> None:
        report = evaluate_gates(gate(metric_key="acc", minimum=0.8), [metric("acc", 0.85)])
        assert report.verdict is Verdict.PASS
        assert report.exit_code == 0

    async def test_minimum_fails(self) -> None:
        report = evaluate_gates(gate(metric_key="acc", minimum=0.8), [metric("acc", 0.75)])
        assert report.verdict is Verdict.FAIL
        assert report.exit_code == 1
        assert "0.75 < minimum 0.8" in report.results[0].message

    async def test_maximum_fails(self) -> None:
        report = evaluate_gates(gate(metric_key="cost", maximum=0.02), [metric("cost", 0.031)])
        assert report.verdict is Verdict.FAIL
        assert report.results[0].rule == "maximum"

    async def test_zero_tolerance_count(self) -> None:
        """`maximum: 0` must fail on a single violation, not round away."""
        rules = gate(metric_key="unsupported_claims", maximum=0.0)
        assert evaluate_gates(rules, [metric("unsupported_claims", 0.0)]).verdict is Verdict.PASS
        assert evaluate_gates(rules, [metric("unsupported_claims", 0.005)]).verdict is Verdict.FAIL


class TestMissingAndBrokenMetrics:
    async def test_missing_metric_is_error_not_pass(self) -> None:
        """A typo'd metric key must never silently pass.

        This is the most dangerous possible gate bug: it produces green CI while
        measuring nothing at all.
        """
        report = evaluate_gates(gate(metric_key="typoed_name", minimum=0.9), [metric("acc", 1.0)])
        assert report.verdict is Verdict.ERROR
        assert report.exit_code == 2
        assert "does not exist cannot pass" in report.results[0].message
        assert "acc" in report.results[0].message  # suggests what *is* available

    async def test_all_evaluations_errored_is_error(self) -> None:
        report = evaluate_gates(
            gate(metric_key="judge", minimum=0.9), [metric("judge", 0.0, n=0, errors=50)]
        )
        assert report.verdict is Verdict.ERROR
        assert "Nothing was measured" in report.results[0].message

    async def test_high_error_rate_is_error_not_pass(self) -> None:
        """A metric computed from mostly-failed evaluations is not trustworthy.

        Ten good scores averaging 0.95 alongside ninety timeouts is not a 0.95.
        """
        report = evaluate_gates(
            gate(metric_key="judge", minimum=0.9), [metric("judge", 0.95, n=10, errors=90)]
        )
        assert report.verdict is Verdict.ERROR
        assert report.results[0].rule == "error_rate"

    async def test_error_rate_within_tolerance_still_gates_normally(self) -> None:
        report = evaluate_gates(
            gate(metric_key="judge", minimum=0.9), [metric("judge", 0.95, n=99, errors=1)]
        )
        assert report.verdict is Verdict.PASS


class TestRegression:
    async def test_absolute_regression_fails(self) -> None:
        report = evaluate_gates(
            gate(metric_key="q", max_absolute_regression=0.02),
            [metric("q", 0.90)],
            [metric("q", 0.95)],
        )
        assert report.verdict is Verdict.FAIL
        assert report.results[0].rule == "max_absolute_regression"

    async def test_improvement_never_fails(self) -> None:
        report = evaluate_gates(
            gate(metric_key="q", max_absolute_regression=0.02),
            [metric("q", 0.99)],
            [metric("q", 0.90)],
        )
        assert report.verdict is Verdict.PASS

    async def test_relative_regression(self) -> None:
        report = evaluate_gates(
            gate(metric_key="q", max_relative_regression=0.05),
            [metric("q", 0.80)],
            [metric("q", 1.00)],
        )
        assert report.verdict is Verdict.FAIL

    async def test_no_baseline_skips_regression_check(self) -> None:
        rules = gate(metric_key="q", max_absolute_regression=0.02)
        report = evaluate_gates(rules, [metric("q", 0.5)])
        assert report.verdict is Verdict.PASS
        assert report.results[0].rule == "no_baseline"

    async def test_require_baseline_errors_when_absent(self) -> None:
        report = evaluate_gates(
            gate(metric_key="q", minimum=0.1, require_baseline=True), [metric("q", 0.5)]
        )
        assert report.verdict is Verdict.ERROR


class TestSeverity:
    async def test_warning_does_not_block(self) -> None:
        report = evaluate_gates(
            gate(metric_key="cost", maximum=0.01, severity=Severity.WARN), [metric("cost", 0.05)]
        )
        assert report.verdict is Verdict.WARN
        assert report.exit_code == 0
        assert report.passed
        assert len(report.warnings) == 1

    async def test_blocking_failure_dominates_warning(self) -> None:
        gate_set = GateSet(
            rules=[
                GateRule(metric_key="cost", maximum=0.01, severity=Severity.WARN),
                GateRule(metric_key="acc", minimum=0.9),
            ]
        )
        report = evaluate_gates(gate_set, [metric("cost", 0.05), metric("acc", 0.5)])
        assert report.verdict is Verdict.FAIL
        assert len(report.blocking_failures) == 1


class TestDatasetMatch:
    async def test_mismatch_blocks_by_default(self) -> None:
        report = evaluate_gates(
            gate(metric_key="acc", minimum=0.1), [metric("acc", 1.0)], dataset_match=False
        )
        assert report.verdict is Verdict.ERROR

    async def test_mismatch_allowed_when_opted_out(self) -> None:
        gate_set = GateSet(
            rules=[GateRule(metric_key="acc", minimum=0.1)], require_dataset_match=False
        )
        report = evaluate_gates(gate_set, [metric("acc", 1.0)], dataset_match=False)
        assert report.verdict is Verdict.PASS


class TestRuleValidation:
    async def test_gate_with_no_condition_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="declares no condition"):
            GateRule(metric_key="acc")

    async def test_contradictory_bounds_rejected(self) -> None:
        with pytest.raises(ValueError, match="no value can satisfy"):
            GateRule(metric_key="acc", minimum=0.9, maximum=0.5)


class TestSlicedGates:
    async def test_slice_targets_the_right_bucket(self) -> None:
        rules = GateSet(
            rules=[
                GateRule(
                    metric_key="per_class_recall",
                    minimum=0.98,
                    slice={"class": "unsubscribe"},
                )
            ]
        )
        metrics = [
            metric("per_class_recall", 0.99, slice={"class": "interested"}),
            metric("per_class_recall", 0.74, slice={"class": "unsubscribe"}),
        ]
        report = evaluate_gates(rules, metrics)
        assert report.verdict is Verdict.FAIL
        assert report.results[0].slice == {"class": "unsubscribe"}
