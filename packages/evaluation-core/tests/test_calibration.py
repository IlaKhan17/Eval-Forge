"""Calibration maths.

The κ tests check against values computed by hand from the textbook formula, written
out in the test so the arithmetic is auditable rather than asserted. A calibration
number that is silently wrong is worse than no calibration, because its whole purpose
is to justify trusting a judge.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from evalforge_core.calibration import (
    MIN_CEILING_EXAMPLES,
    CalibrationRequirement,
    JudgeVerdict,
    LabelledExample,
    PairwiseProbe,
    calibrate,
    check_requirement,
    cohens_kappa,
    confusion_matrix,
    directional_error_rates,
    observed_agreement,
    per_class_breakdown,
    position_bias,
)


def pairs_from_counts(counts: dict[tuple[str, str], int]) -> list[tuple[str, str]]:
    return [pair for pair, count in counts.items() for _ in range(count)]


class TestCohensKappa:
    def test_matches_the_textbook_worked_example(self) -> None:
        # The standard 2x2 example: 50 items, both raters say yes 20 times, both say no
        # 15 times, and they split the remaining 15.
        #   pₒ = (20 + 15) / 50                       = 0.70
        #   pₑ = (25/50)(30/50) + (25/50)(20/50)      = 0.30 + 0.20 = 0.50
        #   κ  = (0.70 - 0.50) / (1 - 0.50)           = 0.40
        pairs = pairs_from_counts(
            {("yes", "yes"): 20, ("yes", "no"): 5, ("no", "yes"): 10, ("no", "no"): 15}
        )
        kappa, undefined = cohens_kappa(pairs)
        assert undefined is None
        assert kappa == pytest.approx(0.4, abs=1e-12)

    def test_same_raw_agreement_different_kappa(self) -> None:
        # Why agreement alone is not evidence. This set agrees 60% of the time, but the
        # marginals are lopsided enough that chance alone explains 54% of it.
        #   pₒ = (45 + 15) / 100                      = 0.60
        #   pₑ = (60/100)(70/100) + (40/100)(30/100)  = 0.42 + 0.12 = 0.54
        #   κ  = 0.06 / 0.46                          = 0.130434...
        pairs = pairs_from_counts(
            {("yes", "yes"): 45, ("yes", "no"): 15, ("no", "yes"): 25, ("no", "no"): 15}
        )
        kappa, _ = cohens_kappa(pairs)
        assert observed_agreement(pairs) == pytest.approx(0.60)
        assert kappa == pytest.approx(0.06 / 0.46, abs=1e-12)

    def test_a_judge_that_always_says_the_majority_class_scores_zero(self) -> None:
        # 90% agreement, no information. This is the failure agreement thresholds miss
        # and the reason κ is the headline number.
        pairs = pairs_from_counts({("spam", "ham"): 10, ("ham", "ham"): 90})
        assert observed_agreement(pairs) == pytest.approx(0.90)
        kappa, undefined = cohens_kappa(pairs)
        assert undefined is None
        assert kappa == pytest.approx(0.0, abs=1e-12)

    def test_perfect_disagreement_is_negative(self) -> None:
        pairs = pairs_from_counts({("yes", "no"): 25, ("no", "yes"): 25})
        kappa, _ = cohens_kappa(pairs)
        assert kappa is not None
        assert kappa == pytest.approx(-1.0)

    def test_undefined_when_both_raters_used_one_label(self) -> None:
        # Chance agreement is 1.0, so κ is 0/0. Reporting 1.0 would certify a judge that
        # answers the same thing every time; reporting 0.0 would reject a judge that was
        # never wrong. Neither is honest.
        kappa, undefined = cohens_kappa([("ok", "ok")] * 40)
        assert kappa is None
        assert undefined is not None
        assert "undefined" in undefined

    def test_undefined_on_an_empty_set(self) -> None:
        kappa, undefined = cohens_kappa([])
        assert kappa is None
        assert undefined == "no labelled examples"

    def test_quadratic_weighting_credits_a_near_miss(self) -> None:
        # order = 1,2,3 so span = 2 and quadratic weights are 1, 0.75, 0.
        # pairs: (1,1)x2, (1,2)x1, (2,2)x1, (3,3)x1   → n = 5
        #   observed = (1·2 + 0.75·1 + 1·1 + 1·1) / 5              = 4.75/5 = 0.95
        #   rows  1→0.6, 2→0.2, 3→0.2      cols 1→0.4, 2→0.4, 3→0.2
        #   expected = 0.24+0.18+0 + 0.06+0.08+0.03 + 0+0.06+0.04  = 0.69
        #   κ = (0.95 - 0.69) / (1 - 0.69)                         = 0.26/0.31
        pairs = [("1", "1"), ("1", "1"), ("1", "2"), ("2", "2"), ("3", "3")]
        order = ["1", "2", "3"]
        weighted, _ = cohens_kappa(pairs, kind="quadratic", order=order)
        assert weighted is not None
        assert weighted == pytest.approx(0.26 / 0.31, abs=1e-12)

        # Unweighted on the same data scores lower, because it treats the off-by-one as
        # a total miss. On a 1-5 rubric that is not a defensible way to grade a scale.
        unweighted, _ = cohens_kappa(pairs, order=order)
        assert unweighted is not None
        assert unweighted < weighted

    def test_weighting_penalises_a_far_miss_more_than_a_near_one(self) -> None:
        order = ["1", "2", "3", "4", "5"]
        near = [("5", "4")] * 10 + [("1", "1")] * 10
        far = [("5", "1")] * 10 + [("1", "1")] * 10
        near_kappa, _ = cohens_kappa(near, kind="quadratic", order=order)
        far_kappa, _ = cohens_kappa(far, kind="quadratic", order=order)
        assert near_kappa is not None
        assert far_kappa is not None
        assert near_kappa > far_kappa

    def test_weighting_requires_an_order(self) -> None:
        with pytest.raises(ValueError, match="ordinal `order`"):
            cohens_kappa([("1", "1")], kind="linear")

    def test_a_label_outside_the_scale_is_undefined_not_averaged(self) -> None:
        # A judge answering "banana" on a 1-5 scale has no position, so no distance.
        # That is a broken judge, not something to average over.
        kappa, undefined = cohens_kappa(
            [("1", "banana"), ("2", "2")], kind="quadratic", order=["1", "2", "3"]
        )
        assert kappa is None
        assert undefined is not None
        assert "outside the declared ordinal scale" in undefined


class TestConfusionMatrix:
    def test_counts_pairs(self) -> None:
        matrix = confusion_matrix([("a", "a"), ("a", "b"), ("b", "b")])
        assert matrix.get("a", "a") == 1
        assert matrix.get("a", "b") == 1
        assert matrix.row_total("a") == 2
        assert matrix.column_total("b") == 2
        assert matrix.total == 3

    def test_includes_a_label_only_the_judge_used(self) -> None:
        # A judge inventing a label outside the schema must appear as a column rather
        # than vanish while still counting as a disagreement.
        matrix = confusion_matrix([("a", "invented")])
        assert "invented" in matrix.labels
        assert matrix.get("a", "invented") == 1

    def test_declared_order_is_preserved(self) -> None:
        matrix = confusion_matrix([("b", "a")], labels=["a", "b", "c"])
        assert matrix.labels == ("a", "b", "c")


class TestDirectionalErrorRates:
    def test_false_pass_is_relative_to_what_humans_failed(self) -> None:
        # 10 human failures, of which the judge waved through 2 → 0.2, not 2/50.
        # The question is "of the defects a human caught, how many would this judge
        # miss?" — dividing by the whole set would flatter a judge on an easy set.
        pairs = pairs_from_counts({("fail", "pass"): 2, ("fail", "fail"): 8, ("pass", "pass"): 40})
        false_pass, false_fail = directional_error_rates(pairs, ["pass"])
        assert false_pass == pytest.approx(0.2)
        assert false_fail == pytest.approx(0.0)

    def test_the_two_directions_are_not_symmetric(self) -> None:
        pairs = pairs_from_counts({("fail", "fail"): 10, ("pass", "fail"): 5, ("pass", "pass"): 15})
        false_pass, false_fail = directional_error_rates(pairs, ["pass"])
        assert false_pass == pytest.approx(0.0)
        assert false_fail == pytest.approx(5 / 20)

    def test_an_empty_denominator_is_unmeasured_not_zero(self) -> None:
        # A calibration set with no negatives cannot show whether the judge catches
        # anything. Reporting 0.0 would let it satisfy `max_false_pass_rate: 0.05`.
        false_pass, false_fail = directional_error_rates([("pass", "pass")] * 30, ["pass"])
        assert false_pass is None
        assert false_fail == pytest.approx(0.0)

    def test_multiple_passing_labels(self) -> None:
        pairs = [("good", "acceptable"), ("bad", "good")]
        false_pass, _ = directional_error_rates(pairs, ["good", "acceptable"])
        assert false_pass == pytest.approx(1.0)


class TestPerClassBreakdown:
    def test_recall_precision_and_top_confusion(self) -> None:
        matrix = confusion_matrix(
            pairs_from_counts(
                {
                    ("unsubscribe", "unsubscribe"): 2,
                    ("unsubscribe", "not_interested"): 8,
                    ("not_interested", "not_interested"): 30,
                }
            )
        )
        classes = {c.label: c for c in per_class_breakdown(matrix)}
        rare = classes["unsubscribe"]
        assert rare.support == 10
        assert rare.recall == pytest.approx(0.2)
        assert rare.precision == pytest.approx(1.0)
        assert rare.top_confusion == ("not_interested", 8)

    def test_a_class_the_judge_never_predicts_has_zero_precision(self) -> None:
        matrix = confusion_matrix([("rare", "common")] * 5)
        classes = {c.label: c for c in per_class_breakdown(matrix)}
        assert classes["rare"].recall == pytest.approx(0.0)
        assert classes["common"].precision == pytest.approx(0.0)
        assert classes["common"].support == 0


class TestPositionBias:
    def test_a_consistent_judge_shows_no_bias(self) -> None:
        probes = [PairwiseProbe(f"p{i}", "a", "b", winner_ab="a", winner_ba="a") for i in range(10)]
        report = position_bias(probes)
        assert report is not None
        assert report.inconsistency_rate == pytest.approx(0.0)
        assert report.first_position_rate == pytest.approx(0.0)
        assert not report.biased

    def test_a_judge_that_always_picks_the_first_option_is_flagged(self) -> None:
        # The canonical pairwise failure: swapping the order swaps the winner every
        # time, so the "ranking" measures presentation order and nothing else.
        probes = [PairwiseProbe(f"p{i}", "a", "b", winner_ab="a", winner_ba="b") for i in range(10)]
        report = position_bias(probes)
        assert report is not None
        assert report.first_position_rate == pytest.approx(1.0)
        assert report.inconsistency_rate == pytest.approx(1.0)
        assert report.biased

    def test_some_inconsistency_is_tolerated(self) -> None:
        # Two similar outputs can genuinely be a tie; a judge is not required to be
        # deterministic on one.
        probes = [PairwiseProbe(f"c{i}", "a", "b", "a", "a") for i in range(9)]
        probes.append(PairwiseProbe("flip", "a", "b", "a", "b"))
        report = position_bias(probes)
        assert report is not None
        assert report.inconsistency_rate == pytest.approx(0.1)
        assert not report.biased

    def test_unresolved_pairs_are_excluded_and_counted(self) -> None:
        # A judge that mostly errors must not look consistent on the two it managed.
        probes = [
            PairwiseProbe("ok", "a", "b", "a", "a"),
            PairwiseProbe("half", "a", "b", "a", None),
            PairwiseProbe("none", "a", "b", None, None),
        ]
        report = position_bias(probes)
        assert report is not None
        assert report.n_pairs == 1
        assert report.n_unresolved == 2

    def test_no_probes_means_no_report(self) -> None:
        assert position_bias([]) is None


def labelled(
    label: str,
    *,
    example_id: str,
    second: str | None = None,
    adjudicated: str | None = None,
    length: int | None = None,
) -> LabelledExample:
    return LabelledExample(
        example_id=example_id,
        human_label=label,
        second_human_label=second,
        adjudicated_label=adjudicated,
        output_length=length,
    )


class TestCalibrate:
    def test_reports_agreement_kappa_and_cost(self) -> None:
        examples = [labelled("pass", example_id=f"e{i}") for i in range(8)]
        examples += [labelled("fail", example_id=f"f{i}") for i in range(8)]
        verdicts = [
            JudgeVerdict(e.example_id, label=e.human_label, cost=Decimal("0.001"), latency_ms=100)
            for e in examples
        ]

        report = calibrate(examples, verdicts, passing_labels=["pass"])
        assert report.n_examples == 16
        assert report.agreement == pytest.approx(1.0)
        assert report.kappa == pytest.approx(1.0)
        assert report.false_pass_rate == pytest.approx(0.0)
        assert report.total_cost == Decimal("0.016")
        assert report.p95_latency_ms == pytest.approx(100.0)

    def test_errored_verdicts_are_excluded_and_counted(self) -> None:
        # A judge timeout is not a disagreement. Counting it as one would turn a
        # provider outage into a failed calibration.
        examples = [labelled("pass", example_id=f"e{i}") for i in range(10)]
        verdicts = [JudgeVerdict("e0", errored=True, error="timeout")]
        verdicts += [JudgeVerdict(e.example_id, label="pass") for e in examples[1:]]

        report = calibrate(examples, verdicts)
        assert report.n_examples == 9
        assert report.n_errored == 1
        assert report.error_rate == pytest.approx(0.1)

    def test_adjudicated_label_wins_over_the_first_annotator(self) -> None:
        # Where two annotators disagreed and a human resolved it, the resolution is
        # ground truth. Scoring the judge against the unadjudicated first pass would
        # penalise it for the annotator's error.
        examples = [
            labelled("fail", example_id="e0", second="pass", adjudicated="pass"),
            labelled("pass", example_id="e1", second="pass"),
        ]
        verdicts = [JudgeVerdict("e0", label="pass"), JudgeVerdict("e1", label="pass")]
        report = calibrate(examples, verdicts)
        assert report.agreement == pytest.approx(1.0)

    def test_unjudged_examples_are_reported_not_hidden(self) -> None:
        examples = [labelled("pass", example_id=f"e{i}") for i in range(10)]
        report = calibrate(examples, [JudgeVerdict("e0", label="pass")])
        assert report.n_examples == 1
        assert any("never judged" in note for note in report.notes)

    def test_a_verdict_for_an_unknown_example_is_ignored_with_a_note(self) -> None:
        report = calibrate(
            [labelled("pass", example_id="e0")],
            [JudgeVerdict("e0", label="pass"), JudgeVerdict("ghost", label="pass")],
        )
        assert report.n_examples == 1
        assert any("absent from the" in note for note in report.notes)

    def test_human_ceiling_is_measured_on_the_same_examples_as_the_judge(self) -> None:
        # The subtle one. The doubly-labelled subset deliberately contains boundary
        # cases, so it is harder than the rest. The judge does well on the easy majority
        # and badly on the hard subset; if the ceiling comparison used the judge's
        # overall κ it would look like it had reached a ceiling it never faced.
        examples: list[LabelledExample] = []
        verdicts: list[JudgeVerdict] = []

        # 40 easy examples, no second annotator, judge always right.
        for i in range(20):
            examples.append(labelled("pass", example_id=f"easy_p{i}"))
            verdicts.append(JudgeVerdict(f"easy_p{i}", label="pass"))
            examples.append(labelled("fail", example_id=f"easy_f{i}"))
            verdicts.append(JudgeVerdict(f"easy_f{i}", label="fail"))

        # 20 hard, doubly-labelled examples where the judge is wrong half the time.
        for i in range(10):
            examples.append(labelled("pass", example_id=f"hard_p{i}", second="pass"))
            verdicts.append(JudgeVerdict(f"hard_p{i}", label="fail"))
            examples.append(labelled("fail", example_id=f"hard_f{i}", second="fail"))
            verdicts.append(JudgeVerdict(f"hard_f{i}", label="fail"))

        report = calibrate(examples, verdicts)
        assert report.n_ceiling_examples == 20
        # Humans agreed perfectly on the hard subset here, so the ceiling is 1.0.
        assert report.human_kappa == pytest.approx(1.0)
        # And the judge's κ on that same subset is far below its overall κ.
        assert report.judge_kappa_on_ceiling_subset is not None
        assert report.kappa is not None
        assert report.judge_kappa_on_ceiling_subset < report.kappa
        assert not report.at_human_ceiling

    def test_a_judge_at_the_ceiling_is_recognised(self) -> None:
        # Humans disagree as much as the judge does, so there is nothing left to fix in
        # the judge — the rubric is the problem.
        examples: list[LabelledExample] = []
        verdicts: list[JudgeVerdict] = []
        for i in range(10):
            # Annotators split on these.
            examples.append(labelled("pass", example_id=f"a{i}", second="fail"))
            verdicts.append(JudgeVerdict(f"a{i}", label="fail"))
            examples.append(labelled("fail", example_id=f"b{i}", second="fail"))
            verdicts.append(JudgeVerdict(f"b{i}", label="fail"))

        report = calibrate(examples, verdicts)
        assert report.at_human_ceiling

    def test_no_second_annotator_says_so_rather_than_inventing_a_ceiling(self) -> None:
        examples = [labelled("pass", example_id=f"e{i}") for i in range(30)]
        verdicts = [JudgeVerdict(e.example_id, label="pass") for e in examples]
        report = calibrate(examples, verdicts)
        assert report.human_kappa is None
        assert any("no human agreement ceiling" in note for note in report.notes)

    def test_a_thin_ceiling_subset_is_flagged(self) -> None:
        examples = [labelled("pass", example_id="e0", second="pass")]
        examples += [labelled("fail", example_id=f"f{i}") for i in range(5)]
        verdicts = [JudgeVerdict(e.example_id, label=e.human_label) for e in examples]
        report = calibrate(examples, verdicts)
        assert report.n_ceiling_examples < MIN_CEILING_EXAMPLES
        assert any("doubly-labelled" in note for note in report.notes)

    def test_ordinal_scale_uses_weighted_kappa_by_default(self) -> None:
        order = ["1", "2", "3", "4", "5"]
        examples = [labelled("4", example_id=f"e{i}") for i in range(10)]
        examples += [labelled("2", example_id=f"f{i}") for i in range(10)]
        verdicts = [JudgeVerdict(e.example_id, label=e.human_label) for e in examples]
        report = calibrate(examples, verdicts, ordinal_order=order)
        assert report.kappa_kind == "quadratic"

    def test_leniency_and_compression_on_an_ordinal_scale(self) -> None:
        # A judge that clusters on 4 while humans use the whole range: mildly generous
        # on average, and using far less of the scale. That compression is why a real
        # regression can fall below a judge's resolution.
        order = ["1", "2", "3", "4", "5"]
        human_labels = ["1", "2", "3", "4", "5"] * 4
        examples = [labelled(label, example_id=f"e{i}") for i, label in enumerate(human_labels)]
        verdicts = [JudgeVerdict(e.example_id, label="4") for e in examples]

        report = calibrate(examples, verdicts, ordinal_order=order)
        assert report.leniency is not None
        assert report.leniency > 0
        assert report.scale_compression == pytest.approx(0.0)

    def test_verbosity_bias_correlates_length_with_signed_error(self) -> None:
        # Humans rated everything 3. The judge rated long outputs 5 and short ones 1, so
        # length predicts its error perfectly.
        order = ["1", "2", "3", "4", "5"]
        examples: list[LabelledExample] = []
        verdicts: list[JudgeVerdict] = []
        for i in range(10):
            examples.append(labelled("3", example_id=f"long{i}", length=2_000 + i))
            verdicts.append(JudgeVerdict(f"long{i}", label="5"))
            examples.append(labelled("3", example_id=f"short{i}", length=50 + i))
            verdicts.append(JudgeVerdict(f"short{i}", label="1"))

        report = calibrate(examples, verdicts, ordinal_order=order)
        assert report.verbosity_bias is not None
        assert report.verbosity_bias > 0.9

    def test_no_verbosity_bias_without_an_ordinal_scale(self) -> None:
        # There is no signed error on a nominal scale — "spam where the human said ham"
        # has no direction, so the statistic would be meaningless.
        examples = [labelled("ham", example_id="e0", length=100)]
        report = calibrate(examples, [JudgeVerdict("e0", label="spam")])
        assert report.verbosity_bias is None

    def test_kappa_interval_is_reproducible_and_wide_on_a_small_set(self) -> None:
        examples = [labelled("pass", example_id=f"p{i}") for i in range(15)]
        examples += [labelled("fail", example_id=f"f{i}") for i in range(15)]
        verdicts = [JudgeVerdict(e.example_id, label=e.human_label) for e in examples[:28]]
        verdicts += [JudgeVerdict(e.example_id, label="pass") for e in examples[28:]]

        first = calibrate(examples, verdicts, bootstrap_resamples=500)
        second = calibrate(examples, verdicts, bootstrap_resamples=500)
        assert first.kappa_ci == second.kappa_ci
        assert first.kappa_ci is not None
        low, high = first.kappa_ci
        assert low <= (first.kappa or 0) <= high


class TestCheckRequirement:
    def build(self, *, n_pass: int = 60, n_fail: int = 60, wrong: int = 0) -> object:
        examples: list[LabelledExample] = []
        verdicts: list[JudgeVerdict] = []
        for i in range(n_pass):
            examples.append(labelled("pass", example_id=f"p{i}", second="pass"))
            verdicts.append(JudgeVerdict(f"p{i}", label="pass"))
        for i in range(n_fail):
            examples.append(labelled("fail", example_id=f"f{i}", second="fail"))
            # `wrong` of the human failures get waved through — false passes.
            verdicts.append(JudgeVerdict(f"f{i}", label="pass" if i < wrong else "fail"))
        return calibrate(examples, verdicts, passing_labels=["pass"])

    def test_a_good_judge_satisfies_the_default_requirement(self) -> None:
        report = self.build()
        check = check_requirement(report, CalibrationRequirement())  # type: ignore[arg-type]
        assert check.satisfied
        assert check.failures == ()

    def test_a_small_set_cannot_certify_anything(self) -> None:
        # The load-bearing control. Without it, `κ = 0.81 ≥ 0.80, passed` from twelve
        # examples reads as evidence when its interval is ±0.4 wide.
        report = self.build(n_pass=6, n_fail=6)
        check = check_requirement(report, CalibrationRequirement())  # type: ignore[arg-type]
        assert not check.satisfied
        assert any("usable labelled examples" in f for f in check.failures)

    def test_a_thin_class_fails_even_when_the_total_is_large(self) -> None:
        # The rare class is usually the one that matters, and 500 examples of the common
        # class say nothing about it.
        report = self.build(n_pass=200, n_fail=5)
        check = check_requirement(report, CalibrationRequirement())  # type: ignore[arg-type]
        assert not check.satisfied
        assert any("under 50 examples for class" in f for f in check.failures)

    def test_false_passes_fail_the_dangerous_direction(self) -> None:
        report = self.build(wrong=12)  # 12/60 = 0.20 false-pass rate
        check = check_requirement(report, CalibrationRequirement())  # type: ignore[arg-type]
        assert not check.satisfied
        assert any("false-pass rate" in f for f in check.failures)
        assert any("ships defects" in f for f in check.failures)

    def test_false_fails_are_tolerated_more_than_false_passes(self) -> None:
        # Same numeric error rate, opposite direction: 0.10 passes the false-fail limit
        # of 0.20 while it would fail the false-pass limit of 0.05. The asymmetry is
        # deliberate — one ships a defect, the other annoys someone.
        examples: list[LabelledExample] = []
        verdicts: list[JudgeVerdict] = []
        for i in range(60):
            examples.append(labelled("pass", example_id=f"p{i}", second="pass"))
            verdicts.append(JudgeVerdict(f"p{i}", label="fail" if i < 6 else "pass"))
        for i in range(60):
            examples.append(labelled("fail", example_id=f"f{i}", second="fail"))
            verdicts.append(JudgeVerdict(f"f{i}", label="fail"))

        report = calibrate(examples, verdicts, passing_labels=["pass"])
        assert report.false_fail_rate == pytest.approx(0.1)
        check = check_requirement(report, CalibrationRequirement())
        assert check.satisfied

    def test_undefined_kappa_fails_a_kappa_requirement(self) -> None:
        # A judge that answers "pass" to everything on an all-pass set agrees 100% of
        # the time. It must not satisfy a κ threshold on that basis.
        examples = [labelled("pass", example_id=f"p{i}", second="pass") for i in range(120)]
        verdicts = [JudgeVerdict(e.example_id, label="pass") for e in examples]
        report = calibrate(examples, verdicts, passing_labels=["pass"])

        assert report.agreement == pytest.approx(1.0)
        assert report.kappa is None
        check = check_requirement(report, CalibrationRequirement(min_per_class=1))
        assert not check.satisfied
        assert any("κ could not be computed" in f for f in check.failures)

    def test_the_human_ceiling_downgrades_a_low_kappa_to_a_warning(self) -> None:
        # A task humans cannot agree on is not a task a judge can be held to. Blocking
        # the merge here would be blaming the judge for an unanswerable rubric.
        examples: list[LabelledExample] = []
        verdicts: list[JudgeVerdict] = []
        for i in range(60):
            # Annotators disagree on half the examples in each direction.
            second = "fail" if i % 2 else "pass"
            examples.append(labelled("pass", example_id=f"p{i}", second=second))
            verdicts.append(JudgeVerdict(f"p{i}", label="fail" if i % 2 else "pass"))
            second = "pass" if i % 2 else "fail"
            examples.append(labelled("fail", example_id=f"f{i}", second=second))
            verdicts.append(JudgeVerdict(f"f{i}", label="pass" if i % 2 else "fail"))

        report = calibrate(examples, verdicts, passing_labels=["pass"])
        assert report.kappa is not None
        assert report.at_human_ceiling

        # The directional limits are switched off because this data also has a high
        # false-pass rate; the behaviour under test is the κ downgrade alone. Note that
        # the ceiling excuses κ and nothing else — a judge that waves through work a
        # human rejected still fails, however much the humans disagreed.
        check = check_requirement(
            report,
            CalibrationRequirement(
                min_agreement=0.0,
                min_kappa=0.95,
                max_false_pass_rate=None,
                max_false_fail_rate=None,
            ),
        )
        assert check.satisfied
        assert any("ceiling of the task" in w for w in check.warnings)

        # And with the false-pass limit back on, the ceiling does not rescue it.
        strict = check_requirement(report, CalibrationRequirement(min_agreement=0.0))
        assert not strict.satisfied
        assert any("false-pass rate" in f for f in strict.failures)

    def test_a_straddling_interval_warns_rather_than_certifying(self) -> None:
        # Point estimate clears the bar, interval does not. The honest answer is "label
        # more examples", not "passed".
        examples: list[LabelledExample] = []
        verdicts: list[JudgeVerdict] = []
        for i in range(55):
            examples.append(labelled("pass", example_id=f"p{i}", second="pass"))
            verdicts.append(JudgeVerdict(f"p{i}", label="pass"))
        for i in range(55):
            examples.append(labelled("fail", example_id=f"f{i}", second="fail"))
            verdicts.append(JudgeVerdict(f"f{i}", label="pass" if i < 9 else "fail"))

        report = calibrate(examples, verdicts, passing_labels=["pass"])
        check = check_requirement(
            report,
            CalibrationRequirement(min_agreement=0.90, min_kappa=None, max_false_pass_rate=None),
        )
        assert report.agreement > 0.90
        assert report.agreement_ci[0] < 0.90
        assert check.satisfied
        assert any("straddles the threshold" in w for w in check.warnings)

    def test_a_high_error_rate_invalidates_the_calibration(self) -> None:
        examples = [labelled("pass", example_id=f"p{i}", second="pass") for i in range(60)]
        examples += [labelled("fail", example_id=f"f{i}", second="fail") for i in range(60)]
        verdicts = [
            JudgeVerdict(e.example_id, label=None, errored=True, error="429")
            if i % 4 == 0
            else JudgeVerdict(e.example_id, label=e.human_label)
            for i, e in enumerate(examples)
        ]
        report = calibrate(examples, verdicts, passing_labels=["pass"])
        check = check_requirement(report, CalibrationRequirement(min_per_class=1))
        assert not check.satisfied
        assert any("errored" in f for f in check.failures)

    def test_position_bias_fails_unless_explicitly_allowed(self) -> None:
        examples = [labelled("pass", example_id=f"p{i}", second="pass") for i in range(60)]
        examples += [labelled("fail", example_id=f"f{i}", second="fail") for i in range(60)]
        verdicts = [JudgeVerdict(e.example_id, label=e.human_label) for e in examples]
        probes = [PairwiseProbe(f"x{i}", "a", "b", "a", "b") for i in range(20)]

        report = calibrate(examples, verdicts, passing_labels=["pass"], probes=probes)
        assert not check_requirement(report, CalibrationRequirement()).satisfied
        assert check_requirement(report, CalibrationRequirement(allow_position_bias=True)).satisfied

    def test_scale_compression_warns(self) -> None:
        order = ["1", "2", "3", "4", "5"]
        examples: list[LabelledExample] = []
        verdicts: list[JudgeVerdict] = []
        for i in range(120):
            human = order[i % 5]
            examples.append(labelled(human, example_id=f"e{i}", second=human))
            verdicts.append(JudgeVerdict(f"e{i}", label="4"))

        report = calibrate(examples, verdicts, ordinal_order=order)
        check = check_requirement(
            report,
            CalibrationRequirement(min_agreement=0.0, min_kappa=None, min_per_class=1),
        )
        assert any("spread the humans" in w for w in check.warnings)
