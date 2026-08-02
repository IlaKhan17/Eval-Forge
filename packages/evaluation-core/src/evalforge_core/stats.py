"""Statistics helpers.

Pure Python, no numpy or scipy. The sample sizes involved (n = 50 to 5 000) do not
justify a numeric dependency in a package whose purity is the point, and every
function here is verified against hand-computed or textbook values in the tests.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def mean(values: Sequence[float]) -> float:
    if not values:
        msg = "mean of an empty sequence is undefined"
        raise ValueError(msg)
    return math.fsum(values) / len(values)


def stddev(values: Sequence[float]) -> float:
    """Sample standard deviation (Bessel-corrected). Zero for n < 2."""
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    variance = math.fsum((v - mu) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile, ``q`` in [0, 100].

    Percentiles are why operational metrics are corpus-level rather than means: a
    p95 latency cannot be recovered from per-example averages.
    """
    if not values:
        msg = "percentile of an empty sequence is undefined"
        raise ValueError(msg)
    if not 0 <= q <= 100:
        msg = f"percentile q must be in [0, 100], got {q}"
        raise ValueError(msg)
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (q / 100)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def bootstrap_ci(
    values: Sequence[float],
    *,
    confidence: float = 0.95,
    resamples: int = 10_000,
    seed: int = 42,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the mean.

    Seeded by default so the same data always yields the same interval — an
    unreproducible confidence interval in a reproducibility tool would be absurd.
    """
    if not values:
        msg = "bootstrap of an empty sequence is undefined"
        raise ValueError(msg)
    if len(values) == 1:
        return values[0], values[0]

    rng = random.Random(seed)  # noqa: S311 — statistical resampling, not security
    n = len(values)
    means: list[float] = []
    for _ in range(resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(math.fsum(sample) / n)
    means.sort()
    alpha = (1 - confidence) / 2
    return (
        means[max(0, int(alpha * resamples) - 1)],
        means[min(resamples - 1, int((1 - alpha) * resamples))],
    )


def wilson_ci(successes: int, total: int, *, confidence: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for a proportion.

    Correct near 0 and 1, where the normal approximation produces intervals that
    extend past the [0,1] bounds — which matters here because the metrics that most
    need an interval (unsubscribe recall, unsupported-claim rate) live at the edges.
    """
    if total <= 0:
        return 0.0, 1.0
    z = _z_for(confidence)
    p = successes / total
    denominator = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))
    return max(0.0, center - margin), min(1.0, center + margin)


def _z_for(confidence: float) -> float:
    table = {0.80: 1.2816, 0.90: 1.6449, 0.95: 1.9600, 0.98: 2.3263, 0.99: 2.5758}
    if confidence in table:
        return table[confidence]
    # Acklam-style inverse normal approximation, adequate for CI widths.
    return math.sqrt(2) * _erfinv(confidence)


def _erfinv(x: float) -> float:
    a = 0.147
    ln = math.log(1 - x**2) if abs(x) < 1 else -30.0
    term = 2 / (math.pi * a) + ln / 2
    return math.copysign(math.sqrt(math.sqrt(term**2 - ln / a) - term), x)


def delta_ci(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    confidence: float = 0.95,
    resamples: int = 5_000,
    seed: int = 42,
) -> tuple[float, float]:
    """Bootstrap CI on the difference of means (candidate minus baseline)."""
    if not baseline or not candidate:
        return 0.0, 0.0
    rng = random.Random(seed)  # noqa: S311 — statistical resampling, not security
    nb, nc = len(baseline), len(candidate)
    deltas: list[float] = []
    for _ in range(resamples):
        b = math.fsum(baseline[rng.randrange(nb)] for _ in range(nb)) / nb
        c = math.fsum(candidate[rng.randrange(nc)] for _ in range(nc)) / nc
        deltas.append(c - b)
    deltas.sort()
    alpha = (1 - confidence) / 2
    return (
        deltas[max(0, int(alpha * resamples) - 1)],
        deltas[min(resamples - 1, int((1 - alpha) * resamples))],
    )
