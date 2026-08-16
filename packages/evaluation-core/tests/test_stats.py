"""Statistics tests — verified against hand-computed and textbook values.

These are checked against values computed independently, never against our own
implementation. A metric library that only agrees with itself is worthless.
"""

from __future__ import annotations

import math

import pytest

from proofstep_core.stats import (
    bootstrap_ci,
    delta_ci,
    mean,
    percentile,
    stddev,
    wilson_ci,
)


class TestMeanAndStddev:
    def test_mean(self) -> None:
        assert mean([1, 2, 3, 4]) == 2.5

    def test_mean_of_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="undefined"):
            mean([])

    def test_stddev_is_bessel_corrected(self) -> None:
        # [2,4,4,4,5,5,7,9]: population sd = 2, sample sd = sqrt(32/7) = 2.13809
        assert stddev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.13809, abs=1e-5)

    def test_stddev_of_single_value_is_zero(self) -> None:
        assert stddev([5.0]) == 0.0


class TestPercentile:
    def test_median_of_odd_count(self) -> None:
        assert percentile([1, 2, 3, 4, 5], 50) == 3

    def test_median_of_even_count_interpolates(self) -> None:
        assert percentile([1, 2, 3, 4], 50) == 2.5

    def test_bounds(self) -> None:
        values = [10, 20, 30, 40, 50]
        assert percentile(values, 0) == 10
        assert percentile(values, 100) == 50

    def test_p95_interpolation(self) -> None:
        # position = (10-1)*0.95 = 8.55 -> between index 8 (90) and 9 (100)
        assert percentile(list(range(10, 101, 10)), 95) == pytest.approx(95.5)

    @pytest.mark.parametrize("q", [-1, 101])
    def test_out_of_range_rejected(self, q: float) -> None:
        with pytest.raises(ValueError, match=r"\[0, 100\]"):
            percentile([1, 2, 3], q)


class TestBootstrap:
    def test_is_reproducible_under_a_fixed_seed(self) -> None:
        values = [0.1 * i for i in range(20)]
        assert bootstrap_ci(values, seed=7) == bootstrap_ci(values, seed=7)

    def test_interval_brackets_the_mean(self) -> None:
        values = [0.8, 0.9, 0.85, 0.95, 0.7, 0.88, 0.92, 0.79]
        low, high = bootstrap_ci(values, resamples=2000)
        assert low <= mean(values) <= high

    def test_constant_data_gives_a_degenerate_interval(self) -> None:
        low, high = bootstrap_ci([0.5] * 10, resamples=500)
        assert low == high == 0.5

    def test_more_data_narrows_the_interval(self) -> None:
        narrow = bootstrap_ci([0.5, 0.6] * 100, resamples=2000)
        wide = bootstrap_ci([0.5, 0.6] * 5, resamples=2000)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


class TestWilson:
    def test_matches_published_value(self) -> None:
        # 10 of 20 at 95%: (0.299298, 0.700702), computed from the closed form
        # with z = 1.959964. Symmetric about 0.5, as it must be for p = 0.5.
        low, high = wilson_ci(10, 20)
        assert low == pytest.approx(0.299298, abs=2e-5)
        assert high == pytest.approx(0.700702, abs=2e-5)
        assert (low + high) / 2 == pytest.approx(0.5, abs=1e-9)

    def test_stays_within_bounds_at_the_extremes(self) -> None:
        """Where the normal approximation would escape [0,1].

        This is the case that matters: unsubscribe recall and unsupported-claim
        rate both live at the edges, which is exactly where the naive interval is
        wrong.
        """
        low, high = wilson_ci(0, 30)
        assert low == 0.0
        assert 0 < high < 1

        low, high = wilson_ci(30, 30)
        assert high == 1.0
        assert 0 < low < 1

    def test_zero_total_is_maximally_uncertain(self) -> None:
        assert wilson_ci(0, 0) == (0.0, 1.0)


class TestDeltaCi:
    def test_identical_samples_bracket_zero(self) -> None:
        values = [0.5, 0.6, 0.7, 0.8, 0.9]
        low, high = delta_ci(values, values, resamples=2000)
        assert low <= 0 <= high

    def test_large_shift_excludes_zero(self) -> None:
        baseline = [0.1] * 50
        candidate = [0.9] * 50
        low, _high = delta_ci(baseline, candidate, resamples=1000)
        assert low > 0
        assert math.isclose(low, 0.8, abs_tol=1e-9)

    def test_empty_input_is_neutral(self) -> None:
        assert delta_ci([], [1.0]) == (0.0, 0.0)
