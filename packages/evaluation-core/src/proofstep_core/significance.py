"""Is this change worse, or is it noise?

Every gate in this system so far answers with a threshold: "fail if accuracy dropped more than two
points". A threshold is a statement about what size of change *matters*, and it is necessary. It is
not sufficient, because it says nothing about whether the change is real.

At forty examples, a two-point drop is one example flipping. At four thousand, the same two points
is a fact. A gate that cannot tell those apart does one of two things, both bad: it blocks merges on
coin flips until people route around it, or it is set loose enough to miss the regressions it exists
to catch. This module is the missing half.

## Paired, always

The candidate and the baseline ran the *same examples*. That pairing is the most valuable thing in
the data and the easiest to throw away: comparing two means treats the runs as independent samples
and pays for the variance between examples — which is usually far larger than the variance between
model versions. Comparing per-example *differences* removes it entirely. In practice this is the
difference between needing several hundred examples to detect a real regression and needing a few
dozen.

So every test here matches on `example_id` and works on the differences. An example that ran on only
one side is dropped and counted, never treated as a zero.

## Which test, and why

- **Continuous scores** (accuracy as a mean, a judge's 1-5 rating, latency): a **paired bootstrap**
  of the mean difference. It makes no assumption about the shape of the distribution, which matters
  because eval scores are usually bounded, lumpy, and often bimodal — exactly where a t-test's
  normality assumption is least defensible.
- **Binary outcomes** (passed / failed): **McNemar's exact test** on the discordant pairs. The
  examples that passed both times or failed both times carry no information about a *change*, and
  including them is how a real regression gets diluted into insignificance by a large easy dataset.

## What a non-significant result means

Not "no change". It means this run could not distinguish the change from noise — which is a claim
about the run's size, not about the code. So `minimum_detectable_effect` reports the smallest
regression this dataset could have caught, and the gate engine uses it to refuse to certify a gate
that was never capable of detecting the thing it guards. A green check from an underpowered test is
worse than no check, because it is believed.

## Multiple comparisons

A suite gating twenty metrics at alpha=0.05 expects one false alarm per run from chance alone.
Holm's correction is applied across the tested set — more powerful than Bonferroni, still making
no assumption about dependence between metrics, which matters here because eval metrics computed
over one dataset are heavily correlated.

No scipy. This package is a pure library with no scientific stack, deliberately, so `statistics` and
`math` do the work — the tests below check the results against textbook values.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from proofstep_types import ExampleResult

#: Resamples for the bootstrap. Ten thousand puts the Monte Carlo error on a p-value near 0.05 at
#: about ±0.002 — an order of magnitude below anything a decision turns on, and still under a second
#: for a few hundred examples in pure Python.
DEFAULT_ITERATIONS = 10_000

#: Fixed so a run is reproducible. A gate whose verdict changes when nothing else did is a gate
#: nobody trusts, and "we re-ran it and it passed" is how a real regression gets merged.
DEFAULT_SEED = 42

#: Below this many pairs, no test is reported at all. Five is not a statistical threshold — it is
#: the point below which a p-value is theatre, and reporting one would invite reading it.
MIN_PAIRS = 5

Alternative = Literal["less", "greater", "two-sided"]
TestKind = Literal["paired_bootstrap", "mcnemar_exact", "insufficient_data"]


@dataclass(frozen=True)
class PairedScores:
    """Per-example values from both runs, matched by example id."""

    metric: str
    differences: tuple[float, ...] = ()
    #: Candidate and baseline values, kept alongside the differences so a binary test can count
    #: discordant pairs without re-matching.
    candidate: tuple[float, ...] = ()
    baseline: tuple[float, ...] = ()

    #: Why examples were left out. Reported rather than silently absorbed: a comparison over 12 of
    #: 200 examples is a different claim from one over 200, and the means alone cannot say which.
    only_in_candidate: int = 0
    only_in_baseline: int = 0
    errored: int = 0

    @property
    def n(self) -> int:
        return len(self.differences)

    @property
    def dropped(self) -> int:
        return self.only_in_candidate + self.only_in_baseline + self.errored


@dataclass(frozen=True)
class SignificanceResult:
    """Whether a difference is distinguishable from noise, and what the run could have detected."""

    metric: str
    test: TestKind
    n_pairs: int
    #: Mean of the per-example differences (candidate minus baseline). Negative is a regression
    #: for a metric where higher is better.
    difference: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    p_value: float | None = None
    #: p-value after Holm's correction across the gate set, when one was applied.
    adjusted_p_value: float | None = None
    #: Smallest regression this many examples could have detected at the requested alpha and power.
    #: `None` when there is nothing to estimate it from.
    minimum_detectable_effect: float | None = None
    dropped: int = 0
    notes: tuple[str, ...] = ()

    def is_significant(self, alpha: float = 0.05) -> bool:
        """Significant at alpha, using the corrected p-value when one exists.

        Absent p-values are *not* significant. An undecidable test must never read as a pass or a
        failure; the gate engine handles it as an explicit third state.
        """
        p = self.adjusted_p_value if self.adjusted_p_value is not None else self.p_value
        return p is not None and p <= alpha

    def underpowered_for(self, effect: float) -> bool:
        """True when this run could not have detected a regression of `effect`.

        The question a threshold gate never asks. `max_absolute_regression: 0.02` over 20 examples
        is a promise the data cannot keep, and the honest answer is to say so rather than to pass.
        """
        mde = self.minimum_detectable_effect
        return mde is not None and mde > abs(effect)


def score_by_example(results: Sequence[ExampleResult], metric: str) -> dict[str, float]:
    """Per-example values for one metric, keyed by example id.

    Errored scores are excluded, not zeroed — the invariant the whole system is built on. An
    evaluation that failed is an absence of measurement, and averaging it as zero turns a provider
    outage into a quality regression.
    """
    values: dict[str, float] = {}
    for result in results:
        for score in result.scores:
            if score.metric != metric or score.error is not None:
                continue
            value = score.value
            if value is None and score.passed is not None:
                # A binary evaluator reports `passed` and no numeric value. Mapping it to 1/0 here
                # keeps one code path for both kinds; `is_binary` recovers the distinction.
                value = 1.0 if score.passed else 0.0
            if value is not None:
                values[result.example_id] = float(value)
    return values


def pair(
    candidate: Sequence[ExampleResult], baseline: Sequence[ExampleResult], metric: str
) -> PairedScores:
    """Match the two runs example by example."""
    cand = score_by_example(candidate, metric)
    base = score_by_example(baseline, metric)

    shared = sorted(set(cand) & set(base))
    differences = tuple(cand[key] - base[key] for key in shared)

    # Errored on one side only: present in the results, absent from the values. Counted separately
    # from "not in the other run at all", because they mean different things — one is a flaky
    # evaluator, the other is a changed dataset.
    candidate_ids = {result.example_id for result in candidate}
    baseline_ids = {result.example_id for result in baseline}
    errored = len((candidate_ids & baseline_ids) - set(shared))

    return PairedScores(
        metric=metric,
        differences=differences,
        candidate=tuple(cand[key] for key in shared),
        baseline=tuple(base[key] for key in shared),
        only_in_candidate=len(candidate_ids - baseline_ids),
        only_in_baseline=len(baseline_ids - candidate_ids),
        errored=errored,
    )


def is_binary(values: Sequence[float]) -> bool:
    """Every observation is 0 or 1.

    Decides which test applies. Checked from the data rather than declared by the evaluator, because
    an evaluator's `type` says how it was computed and not how it is distributed — a rubric judge
    restricted to pass/fail is binary whatever it calls itself.
    """
    return bool(values) and all(value in (0.0, 1.0) for value in values)


def paired_bootstrap(
    differences: Sequence[float],
    *,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
    alternative: Alternative = "less",
    confidence: float = 0.95,
) -> tuple[float, float, float, float]:
    """Mean difference, its confidence interval, and a p-value.

    Returns `(mean, ci_low, ci_high, p_value)`. The interval is percentile-based and two-sided
    regardless of `alternative`: an interval is how a reader judges whether an effect *matters*, and
    a one-sided one invites reading a bound that was never estimated.

    The p-value counts resamples on the wrong side of zero, with the (count + 1) / (iterations + 1)
    correction — without it a p-value of exactly 0 is reported for any effect the bootstrap never
    contradicts, which is a claim no finite resampling can support.
    """
    n = len(differences)
    if n == 0:
        msg = "cannot bootstrap an empty sample"
        raise ValueError(msg)

    observed = statistics.fmean(differences)
    # `random`, not `secrets`: this is a Monte Carlo estimate, and the seed is fixed precisely so a
    # run is reproducible. Cryptographic randomness would make the verdict change between identical
    # runs, which is the property being avoided.
    rng = random.Random(seed)  # noqa: S311
    means: list[float] = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(n):
            total += differences[rng.randrange(n)]
        means.append(total / n)

    means.sort()
    tail = (1.0 - confidence) / 2.0
    ci_low = means[max(0, math.floor(tail * iterations) - 1)]
    ci_high = means[min(iterations - 1, math.ceil((1.0 - tail) * iterations) - 1)]

    if alternative == "less":
        extreme = sum(1 for value in means if value >= 0.0)
    elif alternative == "greater":
        extreme = sum(1 for value in means if value <= 0.0)
    else:
        below = sum(1 for value in means if value >= 0.0)
        above = sum(1 for value in means if value <= 0.0)
        extreme = 2 * min(below, above)

    p_value = min(1.0, (extreme + 1) / (iterations + 1))
    return observed, ci_low, ci_high, p_value


def mcnemar_exact(regressed: int, improved: int, *, alternative: Alternative = "less") -> float:
    """Exact McNemar test on discordant pairs.

    `regressed` is the count that passed in the baseline and failed in the candidate; `improved` is
    the reverse. Examples with the same outcome in both runs are deliberately absent: they carry no
    information about a change, and including them is how a real regression gets diluted to nothing
    by a large, easy dataset.

    Exact binomial rather than the chi-square approximation, which is unreliable below about 25
    discordant pairs — and in eval work the discordant count is usually small, because most examples
    behave the same either way.
    """
    n = regressed + improved
    if n == 0:
        # No example changed outcome. Not evidence of no effect; evidence of nothing at all.
        return 1.0

    def tail(at_most: int) -> float:
        # Exact, in integer arithmetic until the final division. Summing floats here loses precision
        # exactly where it matters — a p-value near the alpha someone is gating on.
        ways = sum(math.comb(n, k) for k in range(at_most + 1))
        return float(ways) / float(2**n)

    if alternative == "less":
        # How surprising is it to see this few improvements, if changes were coin flips?
        return min(1.0, tail(improved))
    if alternative == "greater":
        return min(1.0, tail(regressed))
    return min(1.0, 2 * tail(min(regressed, improved)))


def minimum_detectable_effect(
    differences: Sequence[float], *, alpha: float = 0.05, power: float = 0.8
) -> float | None:
    """The smallest true regression this sample could reliably detect.

    The number that turns "not significant" from a shrug into a fact about the run. Standard
    normal-approximation power calculation on the paired differences:

        MDE = (z_alpha + z_power) * sd / sqrt(n)

    Returns `None` only when there are too few pairs to estimate anything.

    A sample with *zero* variance returns 0.0, not `None`. Every example moved by exactly the same
    amount — the signature of a deterministic evaluator — and such a run has no noise to hide a
    regression behind, so any difference at all is detectable. Reporting "cannot estimate" would be
    read as "underpowered" and would fail `require_power` on precisely the most reliable suites,
    which is backwards. Found by running this against the project's own deterministic reference
    suite, where two identical runs produced exactly that case.
    """
    n = len(differences)
    if n < MIN_PAIRS:
        return None
    sd = statistics.stdev(differences)
    if sd == 0.0:
        return 0.0

    normal = statistics.NormalDist()
    z_alpha = normal.inv_cdf(1.0 - alpha)
    z_power = normal.inv_cdf(power)
    return (z_alpha + z_power) * sd / math.sqrt(n)


def analyse(
    paired: PairedScores,
    *,
    alpha: float = 0.05,
    power: float = 0.8,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> SignificanceResult:
    """Run the appropriate test for one metric."""
    notes: list[str] = []
    if paired.only_in_candidate or paired.only_in_baseline:
        notes.append(
            f"{paired.only_in_candidate + paired.only_in_baseline} example(s) ran on only one "
            "side and were excluded from the comparison"
        )
    if paired.errored:
        notes.append(f"{paired.errored} example(s) errored on one side and could not be paired")

    if paired.n < MIN_PAIRS:
        notes.append(
            f"only {paired.n} paired example(s); too few to distinguish a change from noise"
        )
        return SignificanceResult(
            metric=paired.metric,
            test="insufficient_data",
            n_pairs=paired.n,
            dropped=paired.dropped,
            notes=tuple(notes),
        )

    mde = minimum_detectable_effect(paired.differences, alpha=alpha, power=power)

    if is_binary(paired.candidate) and is_binary(paired.baseline):
        pairs = list(zip(paired.candidate, paired.baseline, strict=True))
        regressed = sum(1 for c, b in pairs if b == 1.0 and c == 0.0)
        improved = sum(1 for c, b in pairs if b == 0.0 and c == 1.0)
        p_value = mcnemar_exact(regressed, improved)
        if regressed + improved == 0:
            notes.append("no example changed outcome, so there is nothing to test")
        return SignificanceResult(
            metric=paired.metric,
            test="mcnemar_exact",
            n_pairs=paired.n,
            difference=statistics.fmean(paired.differences),
            p_value=p_value,
            minimum_detectable_effect=mde,
            dropped=paired.dropped,
            notes=tuple(notes),
        )

    difference, ci_low, ci_high, p_value = paired_bootstrap(
        paired.differences, iterations=iterations, seed=seed
    )
    return SignificanceResult(
        metric=paired.metric,
        test="paired_bootstrap",
        n_pairs=paired.n,
        difference=difference,
        ci_low=ci_low,
        ci_high=ci_high,
        p_value=p_value,
        minimum_detectable_effect=mde,
        dropped=paired.dropped,
        notes=tuple(notes),
    )


def holm_adjust(results: Sequence[SignificanceResult]) -> list[SignificanceResult]:
    """Apply Holm's step-down correction across a set of tests.

    A suite gating twenty metrics at alpha=0.05 expects one false alarm per run from chance alone,
    and whoever investigates it learns to ignore the gate. Holm rather than Bonferroni
    because it is uniformly more powerful at the same guarantee, and neither assumes independence —
    which matters here, since metrics computed over one dataset are heavily correlated.

    Results with no p-value pass through untouched. An undecidable test is not a comparison, and
    counting it toward the correction would penalise every other metric for its absence.
    """
    testable = [result for result in results if result.p_value is not None]
    if not testable:
        return list(results)

    order = sorted(testable, key=lambda result: result.p_value or 1.0)
    m = len(order)
    adjusted: dict[str, float] = {}
    running = 0.0
    for index, result in enumerate(order):
        value = min(1.0, (m - index) * (result.p_value or 1.0))
        # Step-down: an adjusted p-value can never decrease as raw p-values increase, or the
        # ordering the correction is built on would contradict itself.
        running = max(running, value)
        adjusted[result.metric] = running

    return [
        (
            result
            if result.metric not in adjusted
            else SignificanceResult(
                metric=result.metric,
                test=result.test,
                n_pairs=result.n_pairs,
                difference=result.difference,
                ci_low=result.ci_low,
                ci_high=result.ci_high,
                p_value=result.p_value,
                adjusted_p_value=adjusted[result.metric],
                minimum_detectable_effect=result.minimum_detectable_effect,
                dropped=result.dropped,
                notes=result.notes,
            )
        )
        for result in results
    ]


def analyse_all(
    candidate: Sequence[ExampleResult],
    baseline: Sequence[ExampleResult],
    metrics: Sequence[str],
    *,
    alpha: float = 0.05,
    power: float = 0.8,
    iterations: int = DEFAULT_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> dict[str, SignificanceResult]:
    """Test every named metric and correct across the set."""
    results = [
        analyse(
            pair(candidate, baseline, metric),
            alpha=alpha,
            power=power,
            iterations=iterations,
            seed=seed,
        )
        for metric in metrics
    ]
    return {result.metric: result for result in holm_adjust(results)}


__all__ = [
    "DEFAULT_ITERATIONS",
    "DEFAULT_SEED",
    "MIN_PAIRS",
    "PairedScores",
    "SignificanceResult",
    "analyse",
    "analyse_all",
    "holm_adjust",
    "is_binary",
    "mcnemar_exact",
    "minimum_detectable_effect",
    "pair",
    "paired_bootstrap",
    "score_by_example",
]
