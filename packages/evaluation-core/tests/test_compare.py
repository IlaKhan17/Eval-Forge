"""Comparison tests, centred on the two refusals."""

from __future__ import annotations

import pytest

from proofstep_core.compare import compare_metrics
from proofstep_types import ExampleResult, Metric, Score


def m(
    key: str,
    value: float,
    *,
    n: int = 50,
    slice: dict[str, str] | None = None,
) -> Metric:
    return Metric(key=key, value=value, count=n, slice=slice)


def r(example_id: str, metric: str, value: float) -> ExampleResult:
    return ExampleResult(example_id=example_id, scores=[Score(metric=metric, value=value)])


class TestDeltas:
    def test_absolute_and_relative(self) -> None:
        comparison = compare_metrics([m("acc", 0.90)], [m("acc", 1.00)])
        delta = comparison.delta_for("acc")
        assert delta is not None
        assert delta.absolute_delta == pytest.approx(-0.10)
        assert delta.relative_delta == pytest.approx(-0.10)

    def test_zero_baseline_leaves_relative_undefined(self) -> None:
        """Dividing by zero would report an infinite regression."""
        delta = compare_metrics([m("x", 0.5)], [m("x", 0.0)]).delta_for("x")
        assert delta is not None
        assert delta.relative_delta is None

    def test_sliced_metrics_are_matched_by_slice(self) -> None:
        comparison = compare_metrics(
            [
                m("recall", 0.2, slice={"class": "unsubscribe"}),
                m("recall", 0.9, slice={"class": "other"}),
            ],
            [
                m("recall", 0.99, slice={"class": "unsubscribe"}),
                m("recall", 0.9, slice={"class": "other"}),
            ],
        )
        rare = comparison.delta_for("recall", **{"class": "unsubscribe"})
        assert rare is not None
        assert rare.absolute_delta == pytest.approx(-0.79)

    def test_regressed_metrics_are_sorted_worst_first(self) -> None:
        comparison = compare_metrics(
            [m("a", 0.9), m("b", 0.5), m("c", 1.0)],
            [m("a", 1.0), m("b", 1.0), m("c", 1.0)],
        )
        assert [d.key for d in comparison.regressed_metrics] == ["b", "a"]


class TestRefusals:
    def test_dataset_mismatch_is_flagged(self) -> None:
        comparison = compare_metrics(
            [m("acc", 0.9)], [m("acc", 0.8)], candidate_hash="abc123", baseline_hash="def456"
        )
        assert comparison.dataset_match is False
        assert any("Dataset content differs" in w for w in comparison.warnings)

    def test_matching_hashes_are_not_flagged(self) -> None:
        comparison = compare_metrics(
            [m("acc", 0.9)], [m("acc", 0.8)], candidate_hash="abc", baseline_hash="abc"
        )
        assert comparison.dataset_match is True
        assert not comparison.warnings

    def test_new_metric_is_reported_not_silently_ignored(self) -> None:
        comparison = compare_metrics([m("acc", 0.9), m("brand_new", 0.5)], [m("acc", 0.9)])
        assert any("only in the candidate" in w for w in comparison.warnings)

    def test_removed_metric_is_reported(self) -> None:
        """A renamed evaluator silently drops its gate; say so."""
        comparison = compare_metrics([m("acc", 0.9)], [m("acc", 0.9), m("gone", 1.0)])
        assert any("only in the baseline" in w for w in comparison.warnings)


class TestExampleLevelChanges:
    def test_matching_is_by_id_not_position(self) -> None:
        candidate = [r("b", "acc", 0.0), r("a", "acc", 1.0)]
        baseline = [r("a", "acc", 1.0), r("b", "acc", 1.0)]
        comparison = compare_metrics(
            [m("acc", 0.5)], [m("acc", 1.0)], candidate_results=candidate, baseline_results=baseline
        )
        assert len(comparison.regressions) == 1
        assert comparison.regressions[0].example_id == "b"

    def test_improvements_are_tracked_separately(self) -> None:
        comparison = compare_metrics(
            [m("acc", 1.0)],
            [m("acc", 0.0)],
            candidate_results=[r("a", "acc", 1.0)],
            baseline_results=[r("a", "acc", 0.0)],
        )
        assert not comparison.regressions
        assert len(comparison.improvements) == 1

    def test_examples_absent_from_the_baseline_are_skipped(self) -> None:
        comparison = compare_metrics(
            [m("acc", 0.0)],
            [m("acc", 1.0)],
            candidate_results=[r("new", "acc", 0.0)],
            baseline_results=[r("old", "acc", 1.0)],
        )
        assert not comparison.regressions

    def test_errored_scores_are_not_counted_as_regressions(self) -> None:
        """A judge timeout in the candidate is not a quality regression."""
        candidate = [ExampleResult(example_id="a", scores=[Score.failure("acc", "timeout")])]
        baseline = [r("a", "acc", 1.0)]
        comparison = compare_metrics(
            [m("acc", 0.0)], [m("acc", 1.0)], candidate_results=candidate, baseline_results=baseline
        )
        assert not comparison.regressions


class TestSignificance:
    def test_significance_is_reported_for_a_large_shift(self) -> None:
        candidate = [r(f"e{i}", "acc", 0.0) for i in range(30)]
        baseline = [r(f"e{i}", "acc", 1.0) for i in range(30)]
        delta = compare_metrics(
            [m("acc", 0.0)],
            [m("acc", 1.0)],
            candidate_results=candidate,
            baseline_results=baseline,
        ).delta_for("acc")
        assert delta is not None
        assert delta.significant is True

    def test_significance_is_absent_when_there_is_too_little_data(self) -> None:
        delta = compare_metrics(
            [m("acc", 0.0)],
            [m("acc", 1.0)],
            candidate_results=[r("a", "acc", 0.0)],
            baseline_results=[r("a", "acc", 1.0)],
        ).delta_for("acc")
        assert delta is not None
        assert delta.significant is None
