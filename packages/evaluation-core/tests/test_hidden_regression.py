"""The hidden-regression scenario — the reason protected metrics exist.

This is a named regression test rather than one case among many in test_gates.py,
because it encodes the central product claim: an aggregate average cannot protect a
rare, severe failure, and a platform that only reports averages will wave through the
exact class of bug that matters most.

The scenario is drawn from the reply-intent suite (docs/REFERENCE_SUITES.md §1.4):

    1 200 replies across 12 intent classes. `unsubscribe` is ~1% of traffic.
    A change destroys unsubscribe detection: recall 0.99 -> 0.20.
    Every other class is unaffected.

The consequence is that the system keeps emailing people who asked to stop, which is
a CAN-SPAM/GDPR violation and real legal exposure — not a quality dip.

The prevalence matters, and the arithmetic is worth being precise about: overall
accuracy falls by `prevalence x recall_drop`. At 1% prevalence that is 0.79pp, well
inside any plausible `max_absolute_regression: 0.02`. At 3% it would be 2.4pp, which
a 2pp gate *would* catch — so the aggregate is not uniformly blind, it is blind below
a prevalence threshold set by the gate. That threshold is invisible to whoever writes
the gate, which is precisely why rare-but-severe classes need their own absolute
floor rather than a carefully chosen aggregate tolerance.
"""

from __future__ import annotations

from collections.abc import Sequence

from proofstep_core.aggregate import aggregate_scores
from proofstep_core.gates import evaluate_gates
from proofstep_types import ExampleResult, GateRule, GateSet, Metric, Score, Severity, Verdict

TOTAL = 1_200
UNSUBSCRIBE_SHARE = 0.01
UNSUBSCRIBE_N = int(TOTAL * UNSUBSCRIBE_SHARE)  # 12
OTHER_N = TOTAL - UNSUBSCRIBE_N  # 1188


def build_results(*, unsubscribe_recall: float, other_accuracy: float) -> list[ExampleResult]:
    """A run where the rare class and the common classes perform differently."""
    results: list[ExampleResult] = []

    for i in range(UNSUBSCRIBE_N):
        correct = i < round(UNSUBSCRIBE_N * unsubscribe_recall)
        results.append(
            ExampleResult(
                example_id=f"unsub-{i}",
                expected={"intent": "unsubscribe"},
                metadata={"intent": "unsubscribe"},
                scores=[
                    Score.binary("intent_accuracy", correct),
                    Score.binary("per_class_recall", correct, slice={"class": "unsubscribe"}),
                ],
            )
        )

    for i in range(OTHER_N):
        correct = i < round(OTHER_N * other_accuracy)
        results.append(
            ExampleResult(
                example_id=f"other-{i}",
                expected={"intent": "interested"},
                metadata={"intent": "interested"},
                scores=[
                    Score.binary("intent_accuracy", correct),
                    Score.binary("per_class_recall", correct, slice={"class": "interested"}),
                ],
            )
        )

    return results


def find(metrics: Sequence[Metric], key: str, **slice_: str) -> Metric:
    for m in metrics:
        if m.key == key and (m.slice or {}) == slice_:
            return m
    msg = f"metric {key}{slice_} not produced"
    raise AssertionError(msg)


BASELINE = build_results(unsubscribe_recall=0.99, other_accuracy=0.94)
CANDIDATE = build_results(unsubscribe_recall=0.20, other_accuracy=0.94)

BASELINE_METRICS = aggregate_scores(BASELINE, confidence_intervals=False)
CANDIDATE_METRICS = aggregate_scores(CANDIDATE, confidence_intervals=False)


def test_the_aggregate_barely_moves() -> None:
    """Establish the premise: the overall number gives almost no signal."""
    before = find(BASELINE_METRICS, "intent_accuracy").value
    after = find(CANDIDATE_METRICS, "intent_accuracy").value

    drop = before - after
    # prevalence (0.01) x recall drop (0.79) = 0.0079
    assert drop < 0.01, f"expected the aggregate to hide the failure, but it dropped {drop:.4f}"


def test_an_aggregate_only_gate_waves_the_regression_through() -> None:
    """A reasonable-looking aggregate gate passes while the system is broken."""
    aggregate_only = GateSet(
        rules=[
            GateRule(metric_key="intent_accuracy", minimum=0.85, max_absolute_regression=0.02),
        ]
    )
    report = evaluate_gates(aggregate_only, CANDIDATE_METRICS, BASELINE_METRICS)

    assert report.verdict is Verdict.PASS
    assert report.exit_code == 0


def test_a_protected_sliced_gate_catches_it() -> None:
    """The same run, with a protected metric, fails and names the cause."""
    protected = GateSet(
        rules=[
            GateRule(metric_key="intent_accuracy", minimum=0.85, max_absolute_regression=0.02),
            GateRule(
                metric_key="per_class_recall",
                slice={"class": "unsubscribe"},
                minimum=0.98,
                severity=Severity.BLOCK,
            ),
        ]
    )
    report = evaluate_gates(protected, CANDIDATE_METRICS, BASELINE_METRICS)

    assert report.verdict is Verdict.FAIL
    assert report.exit_code == 1

    blocking = report.blocking_failures
    assert len(blocking) == 1
    failure = blocking[0]
    assert failure.metric_key == "per_class_recall"
    assert failure.slice == {"class": "unsubscribe"}
    assert failure.rule == "minimum"
    assert failure.actual is not None
    assert failure.actual < 0.25


def test_the_protected_gate_still_passes_a_healthy_run() -> None:
    """The floor must not be so tight that it blocks normal variation."""
    protected = GateSet(
        rules=[
            GateRule(metric_key="per_class_recall", slice={"class": "unsubscribe"}, minimum=0.98),
        ]
    )
    report = evaluate_gates(protected, BASELINE_METRICS, BASELINE_METRICS)
    assert report.verdict is Verdict.PASS


def test_slicing_makes_the_number_visible_even_without_a_gate() -> None:
    """Defence in depth: the rare-class number is reported whether gated or not."""
    metrics = aggregate_scores(CANDIDATE, slice_by=["intent"], confidence_intervals=False)
    sliced = find(metrics, "intent_accuracy", intent="unsubscribe")
    assert sliced.value < 0.25
