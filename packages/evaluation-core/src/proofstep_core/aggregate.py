"""Roll per-example scores up into metrics.

The load-bearing rule here: **errored evaluations are excluded from the mean and
counted separately.** A judge that timed out is not a failing example, and averaging
infrastructure failures in as zeros is the fastest way to make a metric untrustworthy
(docs/EVALUATION_ENGINE.md §1).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from proofstep_core.stats import bootstrap_ci, mean, stddev
from proofstep_types import ExampleResult, Metric, Score

# A slice is identified by its sorted key/value pairs so it can be a dict key.
SliceKey = tuple[tuple[str, str], ...]


def _slice_key(slice_: dict[str, str] | None) -> SliceKey:
    return tuple(sorted(slice_.items())) if slice_ else ()


def _unkey(key: SliceKey) -> dict[str, str] | None:
    return dict(key) if key else None


def aggregate_scores(
    results: Iterable[ExampleResult],
    *,
    slice_by: Sequence[str] = (),
    confidence_intervals: bool = True,
    seed: int = 42,
) -> list[Metric]:
    """Aggregate every score across every result into metrics.

    ``slice_by`` names metadata keys to additionally break each metric down by. This
    is what surfaces a rare-class collapse that the aggregate hides: slicing
    ``per_class_recall`` by ``class`` makes the unsubscribe number *visible* even
    when nobody thought to gate on it.
    """
    buckets: dict[tuple[str, SliceKey], list[float]] = defaultdict(list)
    errors: dict[tuple[str, SliceKey], int] = defaultdict(int)

    for result in results:
        extra = _extra_slices(result, slice_by)
        for score in result.scores:
            own = _slice_key(score.slice)
            for slice_key in {own, *[_merge(own, s) for s in extra]}:
                bucket = (score.metric, slice_key)
                if score.errored:
                    errors[bucket] += 1
                elif score.value is not None:
                    buckets[bucket].append(score.value)

    # A metric that produced nothing but errors must still appear, with count 0 —
    # otherwise a gate on it sees "metric missing" and cannot distinguish a typo
    # from a wholly broken evaluator.
    metrics: list[Metric] = []
    for bucket in sorted(set(buckets) | set(errors)):
        metric_key, slice_key = bucket
        values = buckets.get(bucket, [])
        error_count = errors.get(bucket, 0)
        metrics.append(
            _build_metric(
                metric_key,
                values,
                error_count,
                _unkey(slice_key),
                confidence_intervals=confidence_intervals,
                seed=seed,
            )
        )
    return metrics


def _build_metric(
    key: str,
    values: list[float],
    error_count: int,
    slice_: dict[str, str] | None,
    *,
    confidence_intervals: bool,
    seed: int,
) -> Metric:
    if not values:
        return Metric(key=key, value=0.0, count=0, error_count=error_count, slice=slice_)

    ci_low = ci_high = None
    # Bootstrapping a handful of points produces an interval that says nothing;
    # reporting one anyway would imply precision that isn't there.
    if confidence_intervals and len(values) >= 5:
        ci_low, ci_high = bootstrap_ci(values, seed=seed)

    return Metric(
        key=key,
        value=mean(values),
        count=len(values),
        error_count=error_count,
        stddev=stddev(values),
        ci_low=ci_low,
        ci_high=ci_high,
        slice=slice_,
    )


def _extra_slices(result: ExampleResult, slice_by: Sequence[str]) -> list[SliceKey]:
    keys: list[SliceKey] = []
    for dimension in slice_by:
        value = result.metadata.get(dimension)
        if value is None and result.expected is not None:
            value = result.expected.get(dimension)
        if value is not None:
            keys.append(((dimension, str(value)),))
    return keys


def _merge(a: SliceKey, b: SliceKey) -> SliceKey:
    return tuple(sorted({**dict(a), **dict(b)}.items()))


def scores_for(results: Iterable[ExampleResult], metric_key: str) -> list[float]:
    """Every non-errored value for one metric, in result order.

    Used by comparison to bootstrap a CI on the delta between two runs.
    """
    return [
        score.value
        for result in results
        for score in result.scores
        if score.metric == metric_key and score.counts_toward_mean and score.value is not None
    ]


def group_by_metric(scores: Iterable[Score]) -> dict[str, list[Score]]:
    grouped: dict[str, list[Score]] = defaultdict(list)
    for score in scores:
        grouped[score.metric].append(score)
    return dict(grouped)
