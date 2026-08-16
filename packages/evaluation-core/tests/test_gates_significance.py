"""Gates that know the difference between a regression and a bad day.

A threshold says what size of change *matters*. It cannot say whether the change is real, and at the
sample sizes eval suites actually run at, most measured "regressions" are neither. These tests pin
the three behaviours that follow from taking that seriously:

- a threshold-breaking drop that the data cannot distinguish from noise does not fail the build
- a rule that asked for a test and did not get one is an ERROR, never a pass
- a gate the run could never have failed reports ERROR rather than a green tick
"""

from __future__ import annotations

from evalforge_core.gates import evaluate_gates
from evalforge_core.significance import SignificanceResult
from evalforge_types import GateRule, GateSet, Metric, Verdict


def metric(key: str, value: float, count: int = 200) -> Metric:
    return Metric(key=key, value=value, count=count)


def measured(
    metric_key: str = "accuracy",
    *,
    p_value: float | None = 0.4,
    n_pairs: int = 200,
    mde: float | None = 0.01,
) -> SignificanceResult:
    return SignificanceResult(
        metric=metric_key,
        test="paired_bootstrap",
        n_pairs=n_pairs,
        difference=-0.05,
        p_value=p_value,
        minimum_detectable_effect=mde,
    )


class TestNoiseDoesNotFailTheBuild:
    def test_an_indistinguishable_regression_passes(self) -> None:
        """The behaviour the whole feature exists for.

        A five-point drop that the run cannot separate from noise blocks nothing. Without this, a
        small suite blocks merges on coin flips until somebody adds `--no-verify` to their habits —
        and then the gate that carries the real safety checks is gone too.
        """
        gates = GateSet(
            rules=[GateRule(metric_key="accuracy", max_absolute_regression=0.02, significance=0.05)]
        )
        report = evaluate_gates(
            gates,
            [metric("accuracy", 0.90)],
            [metric("accuracy", 0.95)],
            significance={"accuracy": measured(p_value=0.4)},
        )
        assert report.verdict is Verdict.PASS
        assert report.results[0].rule == "not_significant"
        # The numbers are in the message: "we measured a drop and could not tell it from noise" is
        # a different claim from "nothing moved", and a reader needs to see which one this is.
        assert "cannot distinguish" in report.results[0].message
        assert "p=0.4" in report.results[0].message

    def test_a_significant_regression_still_fails(self) -> None:
        gates = GateSet(
            rules=[GateRule(metric_key="accuracy", max_absolute_regression=0.02, significance=0.05)]
        )
        report = evaluate_gates(
            gates,
            [metric("accuracy", 0.90)],
            [metric("accuracy", 0.95)],
            significance={"accuracy": measured(p_value=0.001)},
        )
        assert report.verdict is Verdict.FAIL
        assert report.results[0].rule == "max_absolute_regression"

    def test_significance_cannot_rescue_a_drop_below_the_threshold(self) -> None:
        # A change can be real and still not matter. The threshold governs materiality; the test
        # only ever stops a *material* drop from failing on noise.
        gates = GateSet(
            rules=[GateRule(metric_key="accuracy", max_absolute_regression=0.10, significance=0.05)]
        )
        report = evaluate_gates(
            gates,
            [metric("accuracy", 0.94)],
            [metric("accuracy", 0.95)],
            significance={"accuracy": measured(p_value=0.0001)},
        )
        assert report.verdict is Verdict.PASS
        assert report.results[0].rule == "regression"

    def test_an_absolute_floor_is_untouched_by_significance(self) -> None:
        """A protected metric does not negotiate.

        `minimum` is checked before any baseline comparison and has nothing to do with noise: the
        claim is "recall on this class must be 0.98", not "must not have got much worse".
        """
        gates = GateSet(rules=[GateRule(metric_key="recall", minimum=0.98, significance=0.05)])
        report = evaluate_gates(
            gates,
            [metric("recall", 0.4)],
            [metric("recall", 0.99)],
            significance={"recall": measured("recall", p_value=0.9)},
        )
        assert report.verdict is Verdict.FAIL
        assert report.results[0].rule == "minimum"


class TestUntestedIsNotPassed:
    def test_a_rule_that_asked_for_a_test_and_got_none_is_an_error(self) -> None:
        """Silently downgrading to a plain threshold would be the worst of both worlds.

        The suite says "only fail on a regression you can demonstrate". If the demonstration is
        unavailable, the honest answer is that the rule could not be decided — not that it passed.
        """
        gates = GateSet(
            rules=[GateRule(metric_key="accuracy", max_absolute_regression=0.02, significance=0.05)]
        )
        report = evaluate_gates(
            gates, [metric("accuracy", 0.90)], [metric("accuracy", 0.95)], significance={}
        )
        assert report.verdict is Verdict.ERROR
        assert report.results[0].rule == "significance_unavailable"

    def test_an_undecidable_test_is_not_significant(self) -> None:
        # `insufficient_data` carries no p-value. Absent must not read as "not significant, so
        # pass" — it reads as untested, which the gate turns into an ERROR above.
        undecidable = SignificanceResult(metric="accuracy", test="insufficient_data", n_pairs=3)
        assert undecidable.is_significant() is False


class TestPower:
    def test_a_gate_that_could_never_fail_reports_error(self) -> None:
        """The question a threshold gate never asks.

        Twelve examples cannot detect a two-point regression. The rule looks satisfied, the build
        goes green, and nobody learns the check was incapable of detecting the thing it names.
        """
        gates = GateSet(
            rules=[
                GateRule(metric_key="accuracy", max_absolute_regression=0.02, require_power=True)
            ]
        )
        report = evaluate_gates(
            gates,
            [metric("accuracy", 0.95)],
            [metric("accuracy", 0.95)],
            significance={"accuracy": measured(n_pairs=12, mde=0.18)},
        )
        assert report.verdict is Verdict.ERROR
        assert report.results[0].rule == "underpowered"
        assert "cannot see what it claims to guard" in report.results[0].message

    def test_a_powered_run_passes_the_check(self) -> None:
        gates = GateSet(
            rules=[
                GateRule(metric_key="accuracy", max_absolute_regression=0.02, require_power=True)
            ]
        )
        report = evaluate_gates(
            gates,
            [metric("accuracy", 0.95)],
            [metric("accuracy", 0.95)],
            significance={"accuracy": measured(n_pairs=400, mde=0.005)},
        )
        assert report.verdict is Verdict.PASS

    def test_power_is_opt_in(self) -> None:
        # Forcing it on every existing gate would be a breaking change dressed up as rigour: on a
        # small suite the honest answer is often "add examples", and that is a decision for the
        # person who owns the suite.
        gates = GateSet(rules=[GateRule(metric_key="accuracy", max_absolute_regression=0.02)])
        report = evaluate_gates(
            gates,
            [metric("accuracy", 0.95)],
            [metric("accuracy", 0.95)],
            significance={"accuracy": measured(n_pairs=12, mde=0.18)},
        )
        assert report.verdict is Verdict.PASS

    def test_missing_power_information_errors_when_required(self) -> None:
        gates = GateSet(
            rules=[
                GateRule(metric_key="accuracy", max_absolute_regression=0.02, require_power=True)
            ]
        )
        report = evaluate_gates(
            gates, [metric("accuracy", 0.95)], [metric("accuracy", 0.95)], significance={}
        )
        assert report.verdict is Verdict.ERROR
        assert report.results[0].rule == "power_unknown"


class TestBackwardCompatibility:
    def test_a_gate_set_with_no_significance_behaves_exactly_as_before(self) -> None:
        # Every existing suite must keep its meaning. The feature is opt-in per rule, and a report
        # produced without any paired data still gates on thresholds.
        gates = GateSet(rules=[GateRule(metric_key="accuracy", max_absolute_regression=0.02)])
        report = evaluate_gates(gates, [metric("accuracy", 0.90)], [metric("accuracy", 0.95)])
        assert report.verdict is Verdict.FAIL
        assert report.results[0].rule == "max_absolute_regression"
