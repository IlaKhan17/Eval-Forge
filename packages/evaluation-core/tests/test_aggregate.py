"""Aggregation tests, centred on the error-vs-zero distinction."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from evalforge_core.aggregate import aggregate_scores, scores_for
from evalforge_types import ExampleResult, Metric, Score


def result(example_id: str, *scores: Score, **metadata: object) -> ExampleResult:
    return ExampleResult(example_id=example_id, scores=list(scores), metadata=metadata)


def only(metrics: Sequence[Metric], key: str, **slice_: str) -> Metric:
    matches = [m for m in metrics if m.key == key and (m.slice or {}) == slice_]
    assert len(matches) == 1, f"expected exactly one {key}{slice_}, got {len(matches)}"
    return matches[0]


class TestErrorsAreNotZeros:
    """The single most important behaviour in this module."""

    def test_errored_scores_are_excluded_from_the_mean(self) -> None:
        results = [
            result("a", Score.binary("judge", True)),
            result("b", Score.binary("judge", True)),
            result("c", Score.failure("judge", "provider timeout")),
        ]
        metric = only(aggregate_scores(results, confidence_intervals=False), "judge")

        # Two successes out of two *measurements* is 1.0. Counting the timeout as a
        # zero would report 0.667 and make a transient outage look like a quality
        # regression.
        assert metric.value == 1.0
        assert metric.count == 2
        assert metric.error_count == 1
        assert metric.error_rate == pytest.approx(1 / 3)

    def test_all_errors_produces_a_visible_zero_count_metric(self) -> None:
        """The metric must still exist, so a gate can report ERROR rather than 'missing'."""
        results = [result("a", Score.failure("judge", "boom"))]
        metric = only(aggregate_scores(results, confidence_intervals=False), "judge")
        assert metric.count == 0
        assert metric.error_count == 1

    def test_score_cannot_carry_both_error_and_value(self) -> None:
        with pytest.raises(ValueError, match="must not contribute a score"):
            Score(metric="judge", value=0.0, error="timeout")


class TestSlicing:
    def test_scores_carry_their_own_slice(self) -> None:
        results = [
            result("a", Score.binary("recall", True, slice={"class": "x"})),
            result("b", Score.binary("recall", False, slice={"class": "y"})),
        ]
        metrics = aggregate_scores(results, confidence_intervals=False)
        assert only(metrics, "recall", **{"class": "x"}).value == 1.0
        assert only(metrics, "recall", **{"class": "y"}).value == 0.0

    def test_slice_by_metadata_produces_both_overall_and_sliced(self) -> None:
        results = [
            result("a", Score.binary("acc", True), segment="smb"),
            result("b", Score.binary("acc", False), segment="enterprise"),
            result("c", Score.binary("acc", True), segment="enterprise"),
        ]
        metrics = aggregate_scores(results, slice_by=["segment"], confidence_intervals=False)

        assert only(metrics, "acc").value == pytest.approx(2 / 3)
        assert only(metrics, "acc", segment="smb").value == 1.0
        assert only(metrics, "acc", segment="enterprise").value == 0.5

    def test_missing_slice_dimension_is_skipped_not_bucketed_as_none(self) -> None:
        results = [result("a", Score.binary("acc", True))]
        metrics = aggregate_scores(results, slice_by=["segment"], confidence_intervals=False)
        assert all(m.slice is None for m in metrics)


class TestBasics:
    def test_empty_input_produces_no_metrics(self) -> None:
        assert aggregate_scores([], confidence_intervals=False) == []

    def test_confidence_interval_needs_enough_points(self) -> None:
        few = [result(str(i), Score.binary("acc", True)) for i in range(3)]
        assert only(aggregate_scores(few), "acc").ci_low is None

        many = [result(str(i), Score.binary("acc", i % 2 == 0)) for i in range(20)]
        assert only(aggregate_scores(many), "acc").ci_low is not None

    def test_aggregation_is_deterministic(self) -> None:
        results = [result(str(i), Score.binary("acc", i % 3 == 0)) for i in range(30)]
        assert aggregate_scores(results) == aggregate_scores(results)

    def test_scores_for_extracts_raw_values_in_order(self) -> None:
        results = [
            result("a", Score(metric="q", value=0.3)),
            result("b", Score.failure("q", "err")),
            result("c", Score(metric="q", value=0.7)),
        ]
        assert scores_for(results, "q") == [0.3, 0.7]
