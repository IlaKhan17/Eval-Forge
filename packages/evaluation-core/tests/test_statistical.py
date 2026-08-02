"""Corpus evaluator tests, checked against hand-computed values."""

from __future__ import annotations

import math

import pytest

from evalforge_core.evaluators.statistical import (
    CalibrationEvaluator,
    ClassificationEvaluator,
    RankingEvaluator,
    average_precision,
    brier_score,
    expected_calibration_error,
    ndcg_at_k,
    reciprocal_rank,
)
from evalforge_types import ExampleResult, Metric


def result(example_id: str, output: object, expected: dict[str, object]) -> ExampleResult:
    return ExampleResult(example_id=example_id, output=output, expected=expected)


def find(metrics: list[Metric], key: str, **slice_: str) -> Metric:
    for m in metrics:
        if m.key == key and (m.slice or {}) == slice_:
            return m
    msg = f"{key}{slice_} not produced; got {[m.full_key for m in metrics]}"
    raise AssertionError(msg)


class TestClassification:
    """Worked example: 10 items, 2 classes.

    predicted/actual pairs — spam is the rare class.
        6 x (ham, ham)        true negative
        1 x (spam, ham)       false positive for spam
        2 x (spam, spam)      true positive for spam
        1 x (ham, spam)       false negative for spam

    spam: TP=2, FP=1, FN=1 -> precision 2/3, recall 2/3, F1 2/3
    ham:  TP=6, FP=1, FN=1 -> precision 6/7, recall 6/7, F1 6/7
    accuracy = 8/10
    macro F1 = (2/3 + 6/7)/2 = 0.76190
    """

    @staticmethod
    def corpus() -> list[ExampleResult]:
        rows = [("ham", "ham")] * 6 + [("spam", "ham")] + [("spam", "spam")] * 2 + [("ham", "spam")]
        return [result(f"ex-{i}", {"intent": p}, {"intent": a}) for i, (p, a) in enumerate(rows)]

    def test_accuracy(self) -> None:
        metrics = ClassificationEvaluator().evaluate_corpus(self.corpus())
        assert find(metrics, "classification_accuracy").value == pytest.approx(0.8)

    def test_per_class_precision_recall_f1(self) -> None:
        metrics = ClassificationEvaluator().evaluate_corpus(self.corpus())
        assert find(
            metrics, "classification_precision", **{"class": "spam"}
        ).value == pytest.approx(2 / 3)
        assert find(metrics, "classification_recall", **{"class": "spam"}).value == pytest.approx(
            2 / 3
        )
        assert find(metrics, "classification_f1", **{"class": "ham"}).value == pytest.approx(6 / 7)

    def test_macro_f1_is_not_the_mean_of_per_example_scores(self) -> None:
        metrics = ClassificationEvaluator().evaluate_corpus(self.corpus())
        assert find(metrics, "classification_macro_f1").value == pytest.approx(0.761904, abs=1e-5)

    def test_micro_f1_equals_accuracy_for_single_label(self) -> None:
        metrics = ClassificationEvaluator(averaging="micro").evaluate_corpus(self.corpus())
        assert find(metrics, "classification_micro_f1").value == pytest.approx(0.8)

    def test_per_class_metrics_are_emitted_even_without_a_gate(self) -> None:
        """A rare-class collapse must at least be visible in the report."""
        metrics = ClassificationEvaluator().evaluate_corpus(self.corpus())
        sliced = [m for m in metrics if m.slice and m.key == "classification_recall"]
        assert {m.slice["class"] for m in sliced if m.slice} == {"ham", "spam"}

    def test_support_is_the_class_count_not_the_corpus_size(self) -> None:
        metrics = ClassificationEvaluator().evaluate_corpus(self.corpus())
        assert find(metrics, "classification_recall", **{"class": "spam"}).count == 3

    def test_empty_corpus_reports_zero_count(self) -> None:
        metrics = ClassificationEvaluator().evaluate_corpus([])
        assert find(metrics, "classification_accuracy").count == 0


class TestRankingPrimitives:
    def test_ndcg_perfect_ranking_is_one(self) -> None:
        assert ndcg_at_k(["a", "b", "c"], {"a", "b"}, 3) == pytest.approx(1.0)

    def test_ndcg_worked_example(self) -> None:
        # relevant at positions 2 and 4 (1-indexed)
        # DCG  = 1/log2(3) + 1/log2(5) = 0.63093 + 0.43068 = 1.06161
        # IDCG = 1/log2(2) + 1/log2(3) = 1.0     + 0.63093 = 1.63093
        # NDCG = 0.65093
        ranked = ["x", "a", "y", "b", "z"]
        assert ndcg_at_k(ranked, {"a", "b"}, 5) == pytest.approx(0.650927, abs=1e-5)

    def test_ndcg_rewards_earlier_placement(self) -> None:
        early = ndcg_at_k(["a", "x", "y"], {"a"}, 3)
        late = ndcg_at_k(["x", "y", "a"], {"a"}, 3)
        assert early > late

    def test_ndcg_with_no_relevant_items_is_zero(self) -> None:
        assert ndcg_at_k(["a"], set(), 3) == 0.0

    def test_reciprocal_rank(self) -> None:
        assert reciprocal_rank(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)
        assert reciprocal_rank(["x"], {"a"}) == 0.0

    def test_average_precision_worked_example(self) -> None:
        # hits at ranks 1 and 3: (1/1 + 2/3)/2 = 0.83333
        assert average_precision(["a", "x", "b"], {"a", "b"}, 10) == pytest.approx(5 / 6)


class TestRankingEvaluator:
    def test_precision_and_recall_at_k(self) -> None:
        corpus = [
            result("ex-1", {"results": ["a", "b", "x"]}, {"relevant": ["a", "b", "c"]}),
        ]
        metrics = RankingEvaluator(k=3).evaluate_corpus(corpus)
        assert find(metrics, "ranking_precision_at_3").value == pytest.approx(2 / 3)
        assert find(metrics, "ranking_recall_at_3").value == pytest.approx(2 / 3)

    def test_dict_items_use_the_id_key(self) -> None:
        corpus = [
            result(
                "ex-1",
                {"results": [{"id": "a"}, {"id": "z"}]},
                {"relevant": [{"id": "a"}]},
            )
        ]
        metrics = RankingEvaluator(k=2, id_key="id").evaluate_corpus(corpus)
        assert find(metrics, "ranking_precision_at_2").value == pytest.approx(0.5)


class TestCalibration:
    def test_perfect_calibration_has_zero_error(self) -> None:
        # 10 predictions at confidence 1.0, all correct
        points = [(1.0, True)] * 10
        assert expected_calibration_error(points) == pytest.approx(0.0)

    def test_overconfidence_is_penalised(self) -> None:
        # claims 0.9 confidence, right half the time -> gap of 0.4
        points = [(0.9, True)] * 5 + [(0.9, False)] * 5
        assert expected_calibration_error(points) == pytest.approx(0.4)

    def test_brier_score_bounds(self) -> None:
        assert brier_score([(1.0, True)]) == 0.0
        assert brier_score([(1.0, False)]) == 1.0
        assert brier_score([(0.5, True), (0.5, False)]) == pytest.approx(0.25)

    def test_evaluator_reads_confidence_from_the_output(self) -> None:
        corpus = [
            result("a", {"intent": "spam", "confidence": 0.9}, {"intent": "spam"}),
            result("b", {"intent": "spam", "confidence": 0.9}, {"intent": "ham"}),
        ]
        metrics = CalibrationEvaluator().evaluate_corpus(corpus)
        assert find(metrics, "calibration_ece").value == pytest.approx(0.4)
        assert find(metrics, "calibration_brier").value == pytest.approx(
            (0.9 - 1) ** 2 / 2 + 0.9**2 / 2
        )

    def test_examples_without_confidence_are_skipped(self) -> None:
        corpus = [result("a", {"intent": "spam"}, {"intent": "spam"})]
        metrics = CalibrationEvaluator().evaluate_corpus(corpus)
        assert find(metrics, "calibration_ece").count == 0


def test_ndcg_is_bounded() -> None:
    """Property: NDCG never exceeds 1, whatever the ranking."""
    ranked = [f"i{i}" for i in range(20)]
    for size in range(1, 10):
        relevant = {f"i{i}" for i in range(size)}
        value = ndcg_at_k(ranked, relevant, 10)
        assert 0.0 <= value <= 1.0 + 1e-12
        assert not math.isnan(value)
