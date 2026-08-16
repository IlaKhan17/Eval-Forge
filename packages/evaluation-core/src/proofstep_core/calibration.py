"""Judge calibration — deciding whether a judge's numbers are measurements.

An LLM judge is a measuring instrument. An uncalibrated instrument produces numbers,
not measurements, and gating a merge on one means blocking engineers on a figure nobody
has checked. This module is the check.

Everything here is pure: labelled examples in, a report out. No provider calls, no
database, no clock. The judge is *run* elsewhere (`calibration_runner.py`); the maths
lives here so it can be verified against hand-computed values.

Four things this module insists on, each because the obvious alternative is wrong:

1. **Agreement alone is not evidence.** On a task where 90 % of examples are one class,
   a judge that always answers that class agrees 90 % of the time and has measured
   nothing. Cohen's κ corrects for chance agreement, so it is the headline number.

2. **The human ceiling bounds the judge.** If two humans agree at κ = 0.6, a judge at
   κ = 0.6 is at the ceiling and further tuning fits noise. A judge is never held to a
   standard the task itself does not support.

3. **False passes and false fails are not interchangeable.** A judge that passes what a
   human failed lets real defects merge. A judge that fails what a human passed erodes
   trust until people bypass the gate. They are reported separately and gated
   separately, because one is a safety problem and the other is an adoption problem.

4. **A small calibration set cannot certify anything.** κ on 30 examples has a
   confidence interval roughly ±0.25 wide. Reporting `κ = 0.81 ≥ 0.80, passed` from
   such a set is false precision, so sample-size floors are part of the requirement
   rather than advice.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from proofstep_core.stats import mean, percentile, stddev, wilson_ci

# Defaults from docs/EVALUATION_ENGINE.md §5: ≥100 examples, ≥50 per class.
MIN_EXAMPLES = 100
MIN_PER_CLASS = 50
# Below this the doubly-labelled subset cannot estimate a human ceiling worth reporting.
MIN_CEILING_EXAMPLES = 20

KappaKind = Literal["unweighted", "linear", "quadratic"]


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class LabelledExample:
    """One human-labelled calibration example.

    Labels are strings even for a 1-5 rubric. One representation covers binary,
    classification, and ordinal judges, and `ordinal_order` is what tells the maths
    that "2" is nearer "3" than to "5" — without it, a judge that answers 4 where a
    human said 5 is penalised exactly as much as one that answers 1.
    """

    example_id: str
    human_label: str
    #: A second independent annotator, on the subset that has one. This is the only
    #: input from which a human ceiling can be computed; without it the report says so
    #: rather than inventing one.
    second_human_label: str | None = None
    #: Adjudicated label where two annotators disagreed and a human resolved it.
    adjudicated_label: str | None = None
    #: Length of the evaluated output, in characters. Enables verbosity-bias detection.
    output_length: int | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def reference_label(self) -> str:
        """The label the judge is scored against.

        Adjudication wins when present: where two annotators disagreed and a human
        resolved it, the resolution is the ground truth, and scoring the judge against
        the unadjudicated first pass would penalise it for the annotator's error.
        """
        return self.adjudicated_label or self.human_label


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """What the judge returned for one calibration example."""

    example_id: str
    label: str | None = None
    errored: bool = False
    error: str | None = None
    cost: Decimal = Decimal(0)
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class PairwiseProbe:
    """One pairwise comparison run in both orders, for position-bias detection.

    `winner_ab` is the item the judge chose when A was presented first; `winner_ba` is
    its choice when the same two items were presented in the opposite order. A judge
    with no position bias returns the same *item* both times.
    """

    example_id: str
    item_a: str
    item_b: str
    winner_ab: str | None
    winner_ba: str | None

    @property
    def consistent(self) -> bool:
        """True when swapping the order did not change the judge's choice."""
        return (
            self.winner_ab is not None
            and self.winner_ba is not None
            and self.winner_ab == self.winner_ba
        )

    @property
    def chose_first_both_times(self) -> bool:
        """True when the judge picked whichever item came first, whatever it was."""
        return self.winner_ab == self.item_a and self.winner_ba == self.item_b


# --------------------------------------------------------------------------- outputs


@dataclass(frozen=True, slots=True)
class ConfusionMatrix:
    """Counts indexed `[human_label][judge_label]`."""

    labels: tuple[str, ...]
    counts: Mapping[tuple[str, str], int]

    def get(self, human: str, judge: str) -> int:
        return self.counts.get((human, judge), 0)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def row_total(self, human: str) -> int:
        return sum(self.get(human, judge) for judge in self.labels)

    def column_total(self, judge: str) -> int:
        return sum(self.get(human, judge) for human in self.labels)


@dataclass(frozen=True, slots=True)
class ClassBreakdown:
    """Per-class behaviour, so a rare class cannot hide inside an average."""

    label: str
    support: int
    recall: float
    precision: float
    #: Which label the judge most often substituted for this one, and how often.
    top_confusion: tuple[str, int] | None = None


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Everything measured about one judge version against one labelled set."""

    n_examples: int
    n_errored: int
    agreement: float
    agreement_ci: tuple[float, float]
    kappa: float | None
    kappa_ci: tuple[float, float] | None
    kappa_kind: KappaKind
    #: Why κ could not be computed, when it could not. Never silently zero.
    kappa_undefined_reason: str | None
    false_pass_rate: float | None
    false_fail_rate: float | None
    confusion: ConfusionMatrix
    per_class: tuple[ClassBreakdown, ...]

    #: κ between two human annotators on the doubly-labelled subset.
    human_kappa: float | None = None
    #: The judge's κ **restricted to that same subset**, which is the only comparable
    #: number — see `_ceiling`.
    judge_kappa_on_ceiling_subset: float | None = None
    n_ceiling_examples: int = 0

    mean_cost: Decimal = Decimal(0)
    total_cost: Decimal = Decimal(0)
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0

    #: Judge mean minus human mean on an ordinal scale. Positive means the judge is
    #: more generous than the humans.
    leniency: float | None = None
    #: Judge spread divided by human spread. Well below 1 means the judge compresses
    #: the scale, so real regressions fall below its resolution.
    scale_compression: float | None = None
    #: Correlation between output length and the judge's signed error. Positive means
    #: longer outputs are scored more generously than the humans scored them.
    verbosity_bias: float | None = None

    position_bias: PositionBiasReport | None = None

    #: Findings that do not fail the calibration but change how it should be read.
    notes: tuple[str, ...] = ()

    @property
    def error_rate(self) -> float:
        attempted = self.n_examples + self.n_errored
        return self.n_errored / attempted if attempted else 0.0

    @property
    def at_human_ceiling(self) -> bool:
        """True when the judge agrees with humans about as well as humans do.

        Compared on the doubly-labelled subset only, and with a tolerance, because two
        κ values from ~20 examples are not distinguishable to two decimal places.
        """
        if self.human_kappa is None or self.judge_kappa_on_ceiling_subset is None:
            return False
        return self.judge_kappa_on_ceiling_subset >= self.human_kappa - 0.05


@dataclass(frozen=True, slots=True)
class PositionBiasReport:
    """Order effects in a pairwise judge."""

    n_pairs: int
    #: Share of pairs where swapping the presentation order changed the winner.
    inconsistency_rate: float
    #: Share of pairs where the judge simply picked whatever came first.
    first_position_rate: float
    n_unresolved: int

    @property
    def biased(self) -> bool:
        """Whether the order effect is large enough to distrust the ranking.

        0.2 rather than 0: some inconsistency is genuine indifference between two
        similar outputs, and a judge is not required to be deterministic on a tie.
        """
        return self.inconsistency_rate > 0.2 or self.first_position_rate > 0.2


# ------------------------------------------------------------------- requirements


@dataclass(frozen=True, slots=True)
class CalibrationRequirement:
    """What a gate set demands before it will trust a judge.

    The defaults are the recommended values from docs/EVALUATION_ENGINE.md §5 for a
    safety-relevant metric. `max_false_pass_rate` is deliberately much tighter than
    `max_false_fail_rate`: a false pass ships a defect, a false fail annoys someone.
    """

    min_agreement: float = 0.8
    min_kappa: float | None = 0.6
    max_false_pass_rate: float | None = 0.05
    max_false_fail_rate: float | None = 0.20
    min_examples: int = MIN_EXAMPLES
    min_per_class: int = MIN_PER_CLASS
    max_error_rate: float = 0.05
    #: Refuse a judge whose order effects make its ranking unreliable.
    allow_position_bias: bool = False


@dataclass(frozen=True, slots=True)
class RequirementCheck:
    satisfied: bool
    failures: tuple[str, ...]
    warnings: tuple[str, ...]


def check_requirement(  # noqa: PLR0912 — one branch per documented failure mode
    report: CalibrationReport, requirement: CalibrationRequirement
) -> RequirementCheck:
    """Decide whether a calibration report satisfies a requirement.

    Failures block. Warnings do not, but they name things that make the numbers less
    trustworthy than they look — chiefly a confidence interval that straddles the
    threshold, which means the honest answer is "label more examples", not "passed".
    """
    failures: list[str] = []
    warnings: list[str] = []

    if report.n_examples < requirement.min_examples:
        failures.append(
            f"only {report.n_examples} usable labelled examples; "
            f"{requirement.min_examples} are required. A judge certified on a small "
            "set is not certified — κ on 30 examples has a ±0.25 interval."
        )

    thin = [c.label for c in report.per_class if c.support < requirement.min_per_class]
    if thin:
        failures.append(
            f"under {requirement.min_per_class} examples for class(es) "
            f"{', '.join(sorted(thin))}. Per-class agreement cannot be measured on a "
            "handful of examples, and the rare class is usually the one that matters."
        )

    if report.error_rate > requirement.max_error_rate:
        failures.append(
            f"{report.error_rate:.1%} of judge calls errored, above the "
            f"{requirement.max_error_rate:.1%} limit. The calibration measured "
            "whichever examples happened to succeed."
        )

    if report.agreement < requirement.min_agreement:
        failures.append(
            f"agreement {report.agreement:.3f} is below the required "
            f"{requirement.min_agreement:.3f}"
        )
    elif report.agreement_ci[0] < requirement.min_agreement:
        warnings.append(
            f"agreement {report.agreement:.3f} clears {requirement.min_agreement:.3f}, "
            f"but its 95% interval [{report.agreement_ci[0]:.3f}, "
            f"{report.agreement_ci[1]:.3f}] straddles the threshold. The set is too "
            "small to resolve the question; label more examples."
        )

    if requirement.min_kappa is not None:
        if report.kappa is None:
            failures.append(
                f"κ could not be computed ({report.kappa_undefined_reason}), so the "
                "agreement figure is uncorrected for chance and cannot be trusted "
                "against a κ threshold."
            )
        elif report.kappa < requirement.min_kappa:
            # The ceiling is the one thing that can excuse a low κ: a task humans
            # cannot agree on is not a task a judge can be held to.
            if report.at_human_ceiling:
                warnings.append(
                    f"κ {report.kappa:.3f} is below the required "
                    f"{requirement.min_kappa:.3f}, but human annotators only reach "
                    f"{report.human_kappa:.3f} on the same examples. The judge is at "
                    "the ceiling of the task; the rubric is the thing to fix, not the "
                    "judge."
                )
            else:
                failures.append(
                    f"κ {report.kappa:.3f} is below the required {requirement.min_kappa:.3f}"
                )

    if (
        requirement.max_false_pass_rate is not None
        and report.false_pass_rate is not None
        and report.false_pass_rate > requirement.max_false_pass_rate
    ):
        failures.append(
            f"false-pass rate {report.false_pass_rate:.3f} exceeds "
            f"{requirement.max_false_pass_rate:.3f}. This judge passes work a "
            "human rejected, which is the direction that ships defects."
        )

    if (
        requirement.max_false_fail_rate is not None
        and report.false_fail_rate is not None
        and report.false_fail_rate > requirement.max_false_fail_rate
    ):
        failures.append(
            f"false-fail rate {report.false_fail_rate:.3f} exceeds "
            f"{requirement.max_false_fail_rate:.3f}. A gate that blocks acceptable "
            "work gets bypassed, and then it protects nothing."
        )

    if (
        report.position_bias is not None
        and report.position_bias.biased
        and not requirement.allow_position_bias
    ):
        failures.append(
            f"order effects: {report.position_bias.inconsistency_rate:.1%} of pairs "
            f"changed winner when swapped, {report.position_bias.first_position_rate:.1%} "
            "simply picked whichever came first. A pairwise ranking from this judge "
            "measures presentation order."
        )

    if report.scale_compression is not None and report.scale_compression < 0.5:
        warnings.append(
            f"the judge uses {report.scale_compression:.0%} of the spread the humans "
            "use. Leniency clustering compresses the range, so a real regression can "
            "fall below the judge's resolution."
        )

    if report.verbosity_bias is not None and abs(report.verbosity_bias) > 0.3:
        direction = "longer" if report.verbosity_bias > 0 else "shorter"
        warnings.append(
            f"verbosity bias {report.verbosity_bias:+.2f}: the judge scores {direction} "
            "outputs more generously than the humans did, independent of quality."
        )

    return RequirementCheck(
        satisfied=not failures, failures=tuple(failures), warnings=tuple(warnings)
    )


# ---------------------------------------------------------------------- the maths


def confusion_matrix(
    pairs: Sequence[tuple[str, str]], *, labels: Sequence[str] | None = None
) -> ConfusionMatrix:
    """Build a matrix from `(human, judge)` label pairs.

    The label set is the union of both raters' labels, not just the humans'. A judge
    that invents a label outside the schema must appear as a column rather than
    vanishing from the matrix while still counting as a disagreement.
    """
    observed = sorted({label for pair in pairs for label in pair})
    ordered = tuple(labels) if labels is not None else tuple(observed)
    if labels is not None:
        ordered = (*ordered, *(label for label in observed if label not in ordered))

    counts: dict[tuple[str, str], int] = {}
    for human, judge in pairs:
        counts[human, judge] = counts.get((human, judge), 0) + 1
    return ConfusionMatrix(labels=ordered, counts=counts)


def observed_agreement(pairs: Sequence[tuple[str, str]]) -> float:
    if not pairs:
        return 0.0
    return sum(1 for human, judge in pairs if human == judge) / len(pairs)


def cohens_kappa(
    pairs: Sequence[tuple[str, str]],
    *,
    kind: KappaKind = "unweighted",
    order: Sequence[str] | None = None,
) -> tuple[float | None, str | None]:
    """Cohen's κ, returning `(value, undefined_reason)`.

    κ = (po - pe) / (1 - pe), where pe is the agreement expected from the raters'
    marginal frequencies. It is `None` — never 0.0, never 1.0 — when pe = 1, which
    happens whenever both raters used exactly one label. That case is a genuine
    degenerate: perfect agreement carrying no information, since chance alone predicts
    it perfectly. Reporting 1.0 there would certify a judge that answers the same thing
    every time, and reporting 0.0 would reject a judge that was never wrong.

    `kind` selects weighting for ordinal scales, which requires `order`. On a 1-5
    rubric, unweighted κ treats "4 where the human said 5" exactly like "1 where the
    human said 5", which is not a defensible way to grade a scale.
    """
    if not pairs:
        return None, "no labelled examples"

    matrix = confusion_matrix(pairs, labels=order)
    n = matrix.total
    labels = matrix.labels

    if kind != "unweighted":
        if order is None:
            msg = f"{kind} weighting needs an explicit ordinal `order`"
            raise ValueError(msg)
        missing = {label for pair in pairs for label in pair} - set(order)
        if missing:
            # A label outside the declared scale has no position, so no distance. That
            # is a broken judge or a broken dataset, not something to average over.
            return None, f"labels outside the declared ordinal scale: {sorted(missing)}"

    weights = _weights(labels, kind)

    observed = 0.0
    expected = 0.0
    for human in labels:
        row = matrix.row_total(human)
        if row == 0:
            continue
        for judge in labels:
            column = matrix.column_total(judge)
            weight = weights[human, judge]
            observed += weight * matrix.get(human, judge) / n
            expected += weight * (row / n) * (column / n)

    if math.isclose(expected, 1.0, abs_tol=1e-12):
        used = sorted({label for pair in pairs for label in pair})
        return None, (
            f"chance agreement is 1.0 because both raters used only {used!r}; "
            "κ is undefined and the agreement figure carries no information"
        )

    return (observed - expected) / (1 - expected), None


def _weights(labels: Sequence[str], kind: KappaKind) -> dict[tuple[str, str], float]:
    """Agreement weights: 1 for a match, less for a near miss, 0 for the extremes."""
    if kind == "unweighted":
        return {(a, b): 1.0 if a == b else 0.0 for a in labels for b in labels}

    positions = {label: index for index, label in enumerate(labels)}
    span = max(1, len(labels) - 1)
    power = 1 if kind == "linear" else 2
    return {
        (a, b): 1.0 - (abs(positions[a] - positions[b]) / span) ** power
        for a in labels
        for b in labels
    }


def directional_error_rates(
    pairs: Sequence[tuple[str, str]], passing: Sequence[str]
) -> tuple[float | None, float | None]:
    """False-pass and false-fail rates under a pass/fail projection.

    - **false pass** = judge passed / human failed, as a share of what humans failed.
      The denominator is what humans failed, not the whole set, because the question is
      "of the defects a human caught, how many would this judge wave through?"
    - **false fail** = judge failed / human passed, as a share of what humans passed.

    Each is `None` when its denominator is empty, because a rate over zero cases is not
    zero — it is unmeasured, and a gate must not read the two the same way.
    """
    passing_set = set(passing)
    human_failed = [(h, j) for h, j in pairs if h not in passing_set]
    human_passed = [(h, j) for h, j in pairs if h in passing_set]

    false_pass = (
        sum(1 for _, judge in human_failed if judge in passing_set) / len(human_failed)
        if human_failed
        else None
    )
    false_fail = (
        sum(1 for _, judge in human_passed if judge not in passing_set) / len(human_passed)
        if human_passed
        else None
    )
    return false_pass, false_fail


def per_class_breakdown(matrix: ConfusionMatrix) -> tuple[ClassBreakdown, ...]:
    """Recall and precision for each human label, plus its worst substitution."""
    out: list[ClassBreakdown] = []
    for label in matrix.labels:
        support = matrix.row_total(label)
        predicted = matrix.column_total(label)
        hits = matrix.get(label, label)

        confusions = [
            (other, matrix.get(label, other)) for other in matrix.labels if other != label
        ]
        confusions.sort(key=lambda item: (-item[1], item[0]))
        top = confusions[0] if confusions and confusions[0][1] > 0 else None

        out.append(
            ClassBreakdown(
                label=label,
                support=support,
                # Zero support means unmeasured, and 0.0 recall would read as "the judge
                # missed all of them". Kept at 0.0 only when there was something to miss.
                recall=hits / support if support else 0.0,
                precision=hits / predicted if predicted else 0.0,
                top_confusion=top,
            )
        )
    return tuple(out)


def position_bias(probes: Sequence[PairwiseProbe]) -> PositionBiasReport | None:
    """Quantify order effects from comparisons run in both orders.

    Pairwise judges without order swapping are measurably biased, which is why the
    probe carries both directions. Unresolved pairs — where either direction failed to
    produce a winner — are excluded from the rates and counted separately, so a judge
    that mostly errors cannot look consistent.
    """
    if not probes:
        return None

    resolved = [p for p in probes if p.winner_ab is not None and p.winner_ba is not None]
    unresolved = len(probes) - len(resolved)
    if not resolved:
        return PositionBiasReport(
            n_pairs=0, inconsistency_rate=0.0, first_position_rate=0.0, n_unresolved=unresolved
        )

    inconsistent = sum(1 for p in resolved if not p.consistent)
    first = sum(1 for p in resolved if p.chose_first_both_times)
    return PositionBiasReport(
        n_pairs=len(resolved),
        inconsistency_rate=inconsistent / len(resolved),
        first_position_rate=first / len(resolved),
        n_unresolved=unresolved,
    )


# ------------------------------------------------------------------ the entry point


def calibrate(
    labelled: Sequence[LabelledExample],
    verdicts: Sequence[JudgeVerdict],
    *,
    passing_labels: Sequence[str] | None = None,
    ordinal_order: Sequence[str] | None = None,
    kappa_kind: KappaKind | None = None,
    probes: Sequence[PairwiseProbe] = (),
    bootstrap_resamples: int = 2_000,
    seed: int = 42,
) -> CalibrationReport:
    """Compute a full calibration report.

    `ordinal_order` turns on scale-aware maths: weighted κ, leniency, compression. Pass
    it for a rubric judge and leave it out for a classifier, where "nearer" is
    meaningless.
    """
    by_id = {example.example_id: example for example in labelled}
    notes: list[str] = []

    usable: list[tuple[LabelledExample, JudgeVerdict]] = []
    errored = 0
    unknown_ids: list[str] = []
    for verdict in verdicts:
        example = by_id.get(verdict.example_id)
        if example is None:
            unknown_ids.append(verdict.example_id)
            continue
        if verdict.errored or verdict.label is None:
            errored += 1
            continue
        usable.append((example, verdict))

    if unknown_ids:
        notes.append(
            f"{len(unknown_ids)} judge verdict(s) referenced example ids absent from the "
            f"labelled set (e.g. {unknown_ids[0]!r}) and were ignored."
        )
    judged_ids = {verdict.example_id for verdict in verdicts}
    unjudged = [e.example_id for e in labelled if e.example_id not in judged_ids]
    if unjudged:
        # Silently reporting agreement over a subset would overstate coverage.
        notes.append(f"{len(unjudged)} labelled example(s) were never judged and are excluded.")

    pairs = [(example.reference_label, verdict.label or "") for example, verdict in usable]
    matrix = confusion_matrix(pairs, labels=ordinal_order)

    kind: KappaKind = kappa_kind or ("quadratic" if ordinal_order else "unweighted")
    kappa, undefined = cohens_kappa(pairs, kind=kind, order=ordinal_order)

    agreement = observed_agreement(pairs)
    hits = sum(1 for human, judge in pairs if human == judge)
    agreement_ci = wilson_ci(hits, len(pairs)) if pairs else (0.0, 1.0)
    kappa_ci = (
        _kappa_ci(pairs, kind=kind, order=ordinal_order, resamples=bootstrap_resamples, seed=seed)
        if kappa is not None
        else None
    )

    false_pass, false_fail = (
        directional_error_rates(pairs, passing_labels) if passing_labels else (None, None)
    )
    if passing_labels and false_pass is None:
        notes.append(
            "no human-failed examples, so the false-pass rate is unmeasured. A "
            "calibration set with no negatives cannot show whether the judge catches "
            "anything."
        )

    human_kappa, judge_on_subset, n_ceiling, ceiling_note = _ceiling(
        usable, kind=kind, order=ordinal_order
    )
    if ceiling_note:
        notes.append(ceiling_note)

    costs = [verdict.cost for _, verdict in usable]
    latencies = [float(verdict.latency_ms) for _, verdict in usable]

    leniency, compression = _scale_effects(pairs, ordinal_order)
    verbosity = _verbosity_bias(usable, ordinal_order)

    return CalibrationReport(
        n_examples=len(usable),
        n_errored=errored,
        agreement=agreement,
        agreement_ci=agreement_ci,
        kappa=kappa,
        kappa_ci=kappa_ci,
        kappa_kind=kind,
        kappa_undefined_reason=undefined,
        false_pass_rate=false_pass,
        false_fail_rate=false_fail,
        confusion=matrix,
        per_class=per_class_breakdown(matrix),
        human_kappa=human_kappa,
        judge_kappa_on_ceiling_subset=judge_on_subset,
        n_ceiling_examples=n_ceiling,
        mean_cost=(sum(costs, Decimal(0)) / len(costs)) if costs else Decimal(0),
        total_cost=sum(costs, Decimal(0)),
        p50_latency_ms=percentile(latencies, 50) if latencies else 0.0,
        p95_latency_ms=percentile(latencies, 95) if latencies else 0.0,
        leniency=leniency,
        scale_compression=compression,
        verbosity_bias=verbosity,
        position_bias=position_bias(probes),
        notes=tuple(notes),
    )


def _kappa_ci(
    pairs: Sequence[tuple[str, str]],
    *,
    kind: KappaKind,
    order: Sequence[str] | None,
    resamples: int,
    seed: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap interval for κ.

    Bootstrapped rather than using the large-sample analytic standard error: the
    analytic formula is only trustworthy well above the sample sizes calibration sets
    actually reach, and resampling 100 pairs 2 000 times costs milliseconds. Seeded, so
    the same set always yields the same interval.
    """
    if len(pairs) < 2:
        return 0.0, 0.0

    rng = random.Random(seed)  # noqa: S311 — resampling, not security
    n = len(pairs)
    values: list[float] = []
    for _ in range(resamples):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        value, _reason = cohens_kappa(sample, kind=kind, order=order)
        # A resample can be degenerate (every draw the same label). Skipping those
        # rather than substituting 0 keeps the interval about κ instead of about how
        # often the bootstrap collapsed.
        if value is not None:
            values.append(value)

    if not values:
        return 0.0, 0.0
    values.sort()
    alpha = (1 - confidence) / 2
    low = values[max(0, int(alpha * len(values)) - 1)]
    high = values[min(len(values) - 1, int((1 - alpha) * len(values)))]
    return low, high


def _ceiling(
    usable: Sequence[tuple[LabelledExample, JudgeVerdict]],
    *,
    kind: KappaKind,
    order: Sequence[str] | None,
) -> tuple[float | None, float | None, int, str | None]:
    """Human-human κ, and the judge's κ on exactly the same examples.

    The restriction matters and is easy to get wrong. Doubly-labelled examples are the
    ones deliberately chosen to include boundary cases, so they are systematically
    harder than the rest. Comparing the judge's κ over the whole set against the
    humans' κ over that hard subset flatters the judge — it would let a judge look like
    it had reached a ceiling it was never measured against.
    """
    subset = [
        (example, verdict) for example, verdict in usable if example.second_human_label is not None
    ]
    if not subset:
        return (
            None,
            None,
            0,
            (
                "no examples carry a second annotator's label, so there is no human "
                "agreement ceiling to compare against. Double-label at least "
                f"{MIN_CEILING_EXAMPLES} examples to find out whether the task is one "
                "humans agree on at all."
            ),
        )

    human_pairs = [(e.human_label, e.second_human_label or "") for e, _ in subset]
    judge_pairs = [(e.reference_label, v.label or "") for e, v in subset]

    human_kappa, _ = cohens_kappa(human_pairs, kind=kind, order=order)
    judge_kappa, _ = cohens_kappa(judge_pairs, kind=kind, order=order)

    note = None
    if len(subset) < MIN_CEILING_EXAMPLES:
        note = (
            f"the human ceiling rests on {len(subset)} doubly-labelled example(s), "
            f"below the {MIN_CEILING_EXAMPLES} needed for it to mean much."
        )
    return human_kappa, judge_kappa, len(subset), note


def _scale_effects(
    pairs: Sequence[tuple[str, str]], order: Sequence[str] | None
) -> tuple[float | None, float | None]:
    """Leniency and scale compression, for ordinal judges only."""
    if not order or not pairs:
        return None, None

    positions = {label: index for index, label in enumerate(order)}
    human = [positions[h] for h, _ in pairs if h in positions]
    judge = [positions[j] for _, j in pairs if j in positions]
    if len(human) != len(pairs) or len(judge) != len(pairs):
        return None, None

    human_spread = stddev(human)
    leniency = mean(judge) - mean(human)
    # A human set with no spread cannot say whether the judge compressed anything.
    compression = (stddev(judge) / human_spread) if human_spread > 0 else None
    return leniency, compression


def _verbosity_bias(
    usable: Sequence[tuple[LabelledExample, JudgeVerdict]], order: Sequence[str] | None
) -> float | None:
    """Correlation between output length and the judge's signed error.

    Signed error, not raw score: a positive correlation between length and *score*
    might just mean longer answers are genuinely better. Correlating length with
    judge-minus-human isolates the part the humans did not agree was quality — which is
    the definition of the bias.
    """
    if not order:
        return None

    positions = {label: index for index, label in enumerate(order)}
    lengths: list[float] = []
    errors: list[float] = []
    for example, verdict in usable:
        if example.output_length is None or verdict.label not in positions:
            continue
        if example.reference_label not in positions:
            continue
        lengths.append(float(example.output_length))
        errors.append(float(positions[verdict.label] - positions[example.reference_label]))

    if len(lengths) < 3:
        return None
    return _pearson(lengths, errors)


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation, or None when either series is constant."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = mean(xs), mean(ys)
    numerator = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = math.sqrt(math.fsum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(math.fsum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return numerator / (dx * dy)
