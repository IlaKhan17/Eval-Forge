"""Is this change worse, or is it noise?

The arithmetic here decides whether a merge is blocked, so it is checked against values computed by
hand or taken from a textbook rather than against whatever the implementation happened to return.
A statistics module that only tests self-consistency is a module that confidently reports the wrong
number forever.
"""

from __future__ import annotations

import math
import statistics as stdlib_statistics

import pytest

from evalforge_core import significance
from evalforge_types import ExampleResult, Score


def result(example_id: str, metric: str, value: float | None, **kwargs: object) -> ExampleResult:
    return ExampleResult(
        example_id=example_id,
        scores=[Score(metric=metric, value=value, **kwargs)],  # type: ignore[arg-type]
    )


class TestPairing:
    def test_examples_are_matched_by_id_not_by_position(self) -> None:
        """The whole value of a paired test is that it compares like with like.

        Matching by position instead would silently compare example 3's candidate score against
        example 7's baseline the moment either run reordered or skipped one — and both runs
        reordering identically is an assumption nothing enforces.
        """
        candidate = [result("b", "acc", 1.0), result("a", "acc", 0.0)]
        baseline = [result("a", "acc", 1.0), result("b", "acc", 1.0)]

        paired = significance.pair(candidate, baseline, "acc")
        assert paired.n == 2
        # a: 0 - 1 = -1, b: 1 - 1 = 0, in sorted-id order.
        assert paired.differences == (-1.0, 0.0)

    def test_an_example_on_one_side_only_is_dropped_and_counted(self) -> None:
        # Never treated as a zero. A dataset that gained an example is not a run where that example
        # scored nothing.
        paired = significance.pair(
            [result("a", "acc", 1.0), result("new", "acc", 1.0)],
            [result("a", "acc", 1.0), result("gone", "acc", 1.0)],
            "acc",
        )
        assert paired.n == 1
        assert paired.only_in_candidate == 1
        assert paired.only_in_baseline == 1

    def test_an_errored_score_breaks_the_pair_and_is_reported(self) -> None:
        """An evaluation that failed is an absence of measurement.

        Scoring it as zero would turn a provider outage into a quality regression — the invariant
        the whole system is built on, and it has to hold here too.
        """
        paired = significance.pair(
            [result("a", "acc", None, error="judge timed out"), result("b", "acc", 1.0)],
            [result("a", "acc", 1.0), result("b", "acc", 1.0)],
            "acc",
        )
        assert paired.n == 1
        assert paired.errored == 1

    def test_a_binary_score_without_a_value_is_read_from_passed(self) -> None:
        paired = significance.pair(
            [ExampleResult(example_id="a", scores=[Score(metric="ok", passed=False)])],
            [ExampleResult(example_id="a", scores=[Score(metric="ok", passed=True)])],
            "ok",
        )
        assert paired.differences == (-1.0,)


class TestPairedBootstrap:
    def test_it_recovers_the_observed_mean_difference(self) -> None:
        differences = [-0.1, -0.2, 0.0, -0.15, -0.05, -0.3]
        mean, low, high, _ = significance.paired_bootstrap(differences)
        assert mean == pytest.approx(stdlib_statistics.fmean(differences))
        assert low < mean < high

    def test_a_consistent_regression_is_significant(self) -> None:
        # Every example got worse. Any test that cannot call this significant is not worth running.
        differences = [-0.2] * 12
        _, _, _, p = significance.paired_bootstrap(differences)
        assert p < 0.01

    def test_noise_around_zero_is_not_significant(self) -> None:
        """The failure mode this module exists to prevent.

        These twelve examples average a small negative number and mean nothing. A threshold gate at
        `max_absolute_regression: 0.02` would block the merge; the test says the data cannot tell.
        """
        differences = [0.3, -0.4, 0.2, -0.3, 0.35, -0.25, 0.1, -0.2, 0.15, -0.3, 0.2, -0.25]
        _, low, high, p = significance.paired_bootstrap(differences)
        assert p > 0.05
        assert low < 0 < high, "the interval should straddle zero"

    def test_the_p_value_is_never_exactly_zero(self) -> None:
        # (count + 1) / (iterations + 1). No finite resampling can support a claim of p = 0, and a
        # zero would read as certainty rather than as "smaller than we can measure".
        _, _, _, p = significance.paired_bootstrap([-1.0] * 30)
        assert p > 0
        assert p == pytest.approx(1 / (significance.DEFAULT_ITERATIONS + 1))

    def test_it_is_reproducible(self) -> None:
        # A gate whose verdict changes when nothing else did is a gate nobody trusts.
        differences = [-0.1, 0.05, -0.2, 0.0, -0.15, 0.1, -0.05]
        first = significance.paired_bootstrap(differences)
        second = significance.paired_bootstrap(differences)
        assert first == second

    def test_an_empty_sample_raises_rather_than_inventing_an_answer(self) -> None:
        with pytest.raises(ValueError, match="empty sample"):
            significance.paired_bootstrap([])


class TestMcNemar:
    def test_it_matches_the_textbook_value(self) -> None:
        """Exact binomial on the discordant pairs.

        With b=1 regression and c=9 improvements, the one-sided probability of seeing at most one
        of ten changes go one way is (C(10,0) + C(10,1)) / 2^10 = 11/1024 = 0.010742…
        """
        assert significance.mcnemar_exact(regressed=9, improved=1) == pytest.approx(11 / 1024)

    def test_concordant_pairs_do_not_dilute_the_result(self) -> None:
        # Only the discordant counts are arguments at all. This is the point of McNemar: a thousand
        # examples that behaved identically say nothing about whether behaviour changed, and
        # including them is how a real regression is averaged into insignificance.
        assert significance.mcnemar_exact(6, 0) == pytest.approx(1 / 64)

    def test_no_change_at_all_is_p_equals_one(self) -> None:
        # Not evidence of no effect — evidence of nothing. p = 1 is the honest encoding.
        assert significance.mcnemar_exact(0, 0) == 1.0

    def test_a_symmetric_split_is_not_significant(self) -> None:
        assert significance.mcnemar_exact(5, 5) > 0.5

    def test_the_direction_matters(self) -> None:
        # Nine regressions and one improvement is alarming; the reverse is good news. A two-sided
        # test would report both identically.
        worse = significance.mcnemar_exact(regressed=9, improved=1, alternative="less")
        better = significance.mcnemar_exact(regressed=1, improved=9, alternative="less")
        assert worse < 0.05
        assert better > 0.5


class TestMinimumDetectableEffect:
    def test_it_matches_the_closed_form(self) -> None:
        # MDE = (z_alpha + z_power) * sd / sqrt(n), with z_0.95 = 1.6449 and z_0.8 = 0.8416.
        differences = [0.1, -0.1] * 25  # sd = 0.1005…, n = 50
        mde = significance.minimum_detectable_effect(differences)
        assert mde is not None

        sd = stdlib_statistics.stdev(differences)
        expected = (1.6448536 + 0.8416212) * sd / math.sqrt(50)
        assert mde == pytest.approx(expected, rel=1e-4)

    def test_more_examples_detect_smaller_effects(self) -> None:
        small = significance.minimum_detectable_effect([0.1, -0.1] * 10)
        large = significance.minimum_detectable_effect([0.1, -0.1] * 100)
        assert small is not None
        assert large is not None
        assert large < small

    def test_no_variation_is_perfectly_sensitive_not_unknown(self) -> None:
        """A deterministic suite has no noise for a regression to hide behind.

        Returning `None` here would read as "underpowered" downstream and fail `require_power` on
        the most reliable suites in existence — backwards. Every example moving by the same amount
        means any difference at all is detectable, so the minimum detectable effect is zero.
        """
        assert significance.minimum_detectable_effect([0.05] * 20) == 0.0
        assert significance.minimum_detectable_effect([0.0] * 20) == 0.0

    def test_a_deterministic_run_is_never_underpowered(self) -> None:
        identical = [result(f"e{i}", "acc", 1.0) for i in range(40)]
        outcome = significance.analyse(significance.pair(identical, identical, "acc"))
        assert outcome.underpowered_for(0.02) is False

    def test_too_few_examples_has_no_estimate(self) -> None:
        assert significance.minimum_detectable_effect([0.1, -0.1]) is None


class TestAnalyse:
    def test_a_binary_metric_uses_mcnemar(self) -> None:
        # Seven examples flipped from pass to fail and none the other way: 1/128, comfortably
        # significant.
        candidate = [result(f"e{i}", "passed", 1.0 if i >= 7 else 0.0) for i in range(20)]
        baseline = [result(f"e{i}", "passed", 1.0) for i in range(20)]

        outcome = significance.analyse(significance.pair(candidate, baseline, "passed"))
        assert outcome.test == "mcnemar_exact"
        assert outcome.p_value is not None
        assert outcome.p_value < 0.05

    def test_a_handful_of_flips_is_not_significant_however_large_the_dataset(self) -> None:
        """Three failures out of a thousand examples is not evidence of a regression.

        It is the single most common false alarm in eval work: a big dataset makes the *percentage*
        move look tiny and trustworthy, when the only thing that actually changed is three examples.
        McNemar reads the three, not the thousand, and reports p = 0.125 — which is the honest
        answer and the one a threshold gate cannot give.
        """
        candidate = [result(f"e{i}", "passed", 1.0 if i >= 3 else 0.0) for i in range(1000)]
        baseline = [result(f"e{i}", "passed", 1.0) for i in range(1000)]

        outcome = significance.analyse(significance.pair(candidate, baseline, "passed"))
        assert outcome.p_value == pytest.approx(0.125)
        assert not outcome.is_significant()

    def test_a_continuous_metric_uses_the_bootstrap(self) -> None:
        candidate = [result(f"e{i}", "rating", 3.0 + (i % 3) * 0.1) for i in range(20)]
        baseline = [result(f"e{i}", "rating", 4.0 + (i % 3) * 0.1) for i in range(20)]

        outcome = significance.analyse(significance.pair(candidate, baseline, "rating"))
        assert outcome.test == "paired_bootstrap"
        assert outcome.difference == pytest.approx(-1.0)
        assert outcome.ci_high is not None
        assert outcome.ci_high < 0

    def test_too_few_pairs_reports_insufficient_data_not_a_p_value(self) -> None:
        """A p-value over three examples is theatre, and reporting one invites reading it."""
        candidate = [result("a", "acc", 0.0), result("b", "acc", 0.0)]
        baseline = [result("a", "acc", 1.0), result("b", "acc", 1.0)]

        outcome = significance.analyse(significance.pair(candidate, baseline, "acc"))
        assert outcome.test == "insufficient_data"
        assert outcome.p_value is None
        assert outcome.is_significant() is False
        assert any("too few" in note for note in outcome.notes)

    def test_dropped_examples_are_reported_in_the_notes(self) -> None:
        # A comparison over 12 of 200 examples is a different claim from one over 200, and the
        # means alone cannot tell a reader which they are looking at.
        candidate = [result(f"e{i}", "acc", 1.0) for i in range(10)]
        baseline = [result(f"e{i}", "acc", 1.0) for i in range(6)]

        outcome = significance.analyse(significance.pair(candidate, baseline, "acc"))
        assert outcome.dropped == 4
        assert any("only one side" in note for note in outcome.notes)

    def test_an_underpowered_run_says_so(self) -> None:
        """The question a threshold gate never asks.

        Six noisy examples cannot detect a two-point regression, so a gate promising to catch one is
        making a promise the data cannot keep.
        """
        candidate = [result(f"e{i}", "acc", 0.5 + (i % 2) * 0.4) for i in range(6)]
        baseline = [result(f"e{i}", "acc", 0.5 + ((i + 1) % 2) * 0.4) for i in range(6)]

        outcome = significance.analyse(significance.pair(candidate, baseline, "acc"))
        assert outcome.underpowered_for(0.02) is True

    def test_a_large_clean_run_is_powered_for_a_small_effect(self) -> None:
        candidate = [result(f"e{i}", "acc", 0.9 + (i % 5) * 0.001) for i in range(400)]
        baseline = [result(f"e{i}", "acc", 0.91 + (i % 5) * 0.001) for i in range(400)]

        outcome = significance.analyse(significance.pair(candidate, baseline, "acc"))
        assert outcome.underpowered_for(0.02) is False


class TestHolmCorrection:
    def make(self, metric: str, p: float) -> significance.SignificanceResult:
        return significance.SignificanceResult(
            metric=metric, test="paired_bootstrap", n_pairs=50, p_value=p
        )

    def test_it_scales_the_smallest_p_by_the_number_of_tests(self) -> None:
        # Holm: the smallest p is multiplied by m, the next by m-1, and so on.
        adjusted = significance.holm_adjust(
            [self.make("a", 0.01), self.make("b", 0.04), self.make("c", 0.30)]
        )
        by_metric = {row.metric: row.adjusted_p_value for row in adjusted}
        assert by_metric["a"] == pytest.approx(0.03)
        assert by_metric["b"] == pytest.approx(0.08)
        assert by_metric["c"] == pytest.approx(0.30)

    def test_it_turns_a_marginal_result_into_a_non_result(self) -> None:
        """Twenty metrics at 0.05 expects one false alarm per run from chance alone.

        Without the correction, the gate that fires on it sends someone to investigate a change that
        never happened — and after the second time, nobody reads the gate.
        """
        results = [self.make(f"m{i}", 0.04) for i in range(20)]
        adjusted = significance.holm_adjust(results)
        assert all(not row.is_significant(0.05) for row in adjusted)

    def test_adjusted_values_never_decrease(self) -> None:
        # The step-down property. Without it the correction can contradict the ordering it is
        # built on, and a metric with a larger raw p can end up looking more significant.
        adjusted = significance.holm_adjust(
            [self.make("a", 0.001), self.make("b", 0.02), self.make("c", 0.021)]
        )
        ordered = sorted(adjusted, key=lambda row: row.p_value or 1.0)
        values = [row.adjusted_p_value or 0.0 for row in ordered]
        assert values == sorted(values)

    def test_undecidable_tests_do_not_penalise_the_others(self) -> None:
        # A test with no p-value is not a comparison. Counting it toward m would make every other
        # metric harder to call significant because one was unmeasurable.
        with_gap = significance.holm_adjust(
            [
                self.make("a", 0.01),
                significance.SignificanceResult(metric="b", test="insufficient_data", n_pairs=2),
            ]
        )
        assert next(r for r in with_gap if r.metric == "a").adjusted_p_value == pytest.approx(0.01)


class TestAnalyseAll:
    def test_it_tests_every_metric_and_corrects_across_them(self) -> None:
        candidate = [
            ExampleResult(
                example_id=f"e{i}",
                scores=[Score(metric="acc", value=0.0), Score(metric="tone", value=3.0)],
            )
            for i in range(30)
        ]
        baseline = [
            ExampleResult(
                example_id=f"e{i}",
                scores=[Score(metric="acc", value=1.0), Score(metric="tone", value=3.0)],
            )
            for i in range(30)
        ]

        outcome = significance.analyse_all(candidate, baseline, ["acc", "tone"])
        assert set(outcome) == {"acc", "tone"}
        # A total collapse is significant even after correction…
        assert outcome["acc"].is_significant()
        # …and an unchanged metric is not.
        assert not outcome["tone"].is_significant()
