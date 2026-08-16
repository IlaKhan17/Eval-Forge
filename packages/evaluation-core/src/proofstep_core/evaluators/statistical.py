"""Corpus-level statistical evaluators.

These implement `CorpusEvaluator`, not `Evaluator`, because they are **not means of
per-example scores**. F1 needs the full confusion matrix; NDCG needs the whole
ranking; ECE needs the full distribution of confidences. Computing "the mean of
per-example F1" is a real and common statistical error that produces a number which
looks plausible and is wrong (docs/EVALUATION_ENGINE.md §1).

Everything here is ground-truth arithmetic. No judge is involved, and none should be:
a classifier's accuracy against human labels is measurable, and asking a model to
opine on it would be slower, costlier, and less correct.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Literal

from proofstep_core.paths import try_resolve
from proofstep_core.stats import mean
from proofstep_types import ExampleResult, Metric

Averaging = Literal["macro", "micro", "weighted"]


class ClassificationEvaluator:
    """Accuracy, precision, recall, F1 and per-class breakdowns.

    Always emits per-class recall as sliced metrics, whether or not anyone gated on
    it. That is deliberate: a rare-class collapse should at minimum be *visible* in
    the report even when the suite author did not think to protect it.
    """

    version = 1

    def __init__(
        self,
        *,
        prediction_field: str = "intent",
        label_field: str | None = None,
        averaging: Averaging = "macro",
        name: str = "classification",
        labels: Sequence[str] | None = None,
    ) -> None:
        self.name = name
        self.prediction_field = prediction_field
        self.label_field = label_field or prediction_field
        self.averaging = averaging
        self.labels = list(labels) if labels else None

    def evaluate_corpus(self, results: Sequence[ExampleResult]) -> list[Metric]:
        pairs = self._pairs(results)
        if not pairs:
            return [Metric(key=f"{self.name}_accuracy", value=0.0, count=0)]

        classes = self.labels or sorted({label for _, label in pairs} | {p for p, _ in pairs})
        matrix = confusion_matrix(pairs, classes)
        support = {c: sum(1 for _, label in pairs if label == c) for c in classes}

        correct = sum(1 for pred, label in pairs if pred == label)
        metrics: list[Metric] = [
            Metric(
                key=f"{self.name}_accuracy",
                value=correct / len(pairs),
                count=len(pairs),
                aggregation="accuracy",
            )
        ]

        per_class: dict[str, tuple[float, float, float]] = {}
        for cls in classes:
            tp = matrix[cls][cls]
            fp = sum(matrix[other][cls] for other in classes if other != cls)
            fn = sum(matrix[cls][other] for other in classes if other != cls)
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
            per_class[cls] = (precision, recall, f1)

            metrics += [
                Metric(
                    key=f"{self.name}_precision",
                    value=precision,
                    count=support[cls],
                    slice={"class": cls},
                ),
                Metric(
                    key=f"{self.name}_recall",
                    value=recall,
                    count=support[cls],
                    slice={"class": cls},
                ),
                Metric(key=f"{self.name}_f1", value=f1, count=support[cls], slice={"class": cls}),
            ]

        metrics += self._averaged(per_class, support, pairs, matrix, classes)
        metrics.append(
            Metric(
                key=f"{self.name}_confusion_matrix",
                value=0.0,
                count=len(pairs),
                aggregation="matrix",
            )
        )
        return metrics

    def _averaged(
        self,
        per_class: dict[str, tuple[float, float, float]],
        support: dict[str, int],
        pairs: list[tuple[str, str]],
        matrix: dict[str, dict[str, int]],
        classes: Sequence[str],
    ) -> list[Metric]:
        n = len(pairs)
        if self.averaging == "macro":
            # Unweighted mean over classes. This is the average that *notices* a rare
            # class, which is why it is the default for classification suites.
            return [
                Metric(
                    key=f"{self.name}_macro_f1",
                    value=mean([f for _, _, f in per_class.values()]),
                    count=n,
                ),
                Metric(
                    key=f"{self.name}_macro_recall",
                    value=mean([r for _, r, _ in per_class.values()]),
                    count=n,
                ),
            ]
        if self.averaging == "weighted":
            total = sum(support.values()) or 1
            return [
                Metric(
                    key=f"{self.name}_weighted_f1",
                    value=sum(per_class[c][2] * support[c] for c in classes) / total,
                    count=n,
                )
            ]
        # Micro-averaged F1 over a single-label problem equals accuracy; report it
        # under its own name so nobody mistakes it for a second, independent signal.
        tp = sum(matrix[c][c] for c in classes)
        return [Metric(key=f"{self.name}_micro_f1", value=tp / n, count=n)]

    def _pairs(self, results: Sequence[ExampleResult]) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for result in results:
            if not result.ok or result.expected is None:
                continue
            predicted = try_resolve(result.output, self.prediction_field)
            actual = result.expected.get(self.label_field)
            if predicted is None or actual is None:
                continue
            pairs.append((str(predicted), str(actual)))
        return pairs


def confusion_matrix(
    pairs: Sequence[tuple[str, str]], classes: Sequence[str]
) -> dict[str, dict[str, int]]:
    """``matrix[actual][predicted]`` counts."""
    matrix: dict[str, dict[str, int]] = {a: dict.fromkeys(classes, 0) for a in classes}
    for predicted, actual in pairs:
        if actual in matrix and predicted in matrix[actual]:
            matrix[actual][predicted] += 1
    return matrix


class RankingEvaluator:
    """Precision@K, Recall@K, NDCG@K, MRR and MAP against a relevant-item set."""

    version = 1

    def __init__(
        self,
        *,
        k: int = 10,
        ranking_field: str = "results",
        relevant_field: str = "relevant",
        id_key: str | None = None,
        name: str = "ranking",
    ) -> None:
        self.name = name
        self.k = k
        self.ranking_field = ranking_field
        self.relevant_field = relevant_field
        self.id_key = id_key

    def evaluate_corpus(self, results: Sequence[ExampleResult]) -> list[Metric]:
        precisions: list[float] = []
        recalls: list[float] = []
        ndcgs: list[float] = []
        rrs: list[float] = []
        aps: list[float] = []

        for result in results:
            if not result.ok or result.expected is None:
                continue
            ranked = self._ids(try_resolve(result.output, self.ranking_field) or [])
            relevant = set(self._ids(result.expected.get(self.relevant_field) or []))
            if not ranked:
                continue

            top = ranked[: self.k]
            hits = sum(1 for item in top if item in relevant)
            precisions.append(hits / len(top))
            recalls.append(hits / len(relevant) if relevant else 0.0)
            ndcgs.append(ndcg_at_k(ranked, relevant, self.k))
            rrs.append(reciprocal_rank(ranked, relevant))
            aps.append(average_precision(ranked, relevant, self.k))

        if not precisions:
            return [Metric(key=f"{self.name}_precision_at_{self.k}", value=0.0, count=0)]

        n = len(precisions)
        return [
            Metric(key=f"{self.name}_precision_at_{self.k}", value=mean(precisions), count=n),
            Metric(key=f"{self.name}_recall_at_{self.k}", value=mean(recalls), count=n),
            Metric(key=f"{self.name}_ndcg_at_{self.k}", value=mean(ndcgs), count=n),
            Metric(key=f"{self.name}_mrr", value=mean(rrs), count=n),
            Metric(key=f"{self.name}_map", value=mean(aps), count=n),
        ]

    def _ids(self, items: Any) -> list[str]:
        out: list[str] = []
        for item in items:
            if self.id_key and isinstance(item, dict):
                out.append(str(item.get(self.id_key)))
            elif isinstance(item, dict):
                out.append(str(item.get("id", item)))
            else:
                out.append(str(item))
        return out


def dcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """Binary-gain DCG with log2(rank + 1) discount."""
    return math.fsum(
        1.0 / math.log2(rank + 2) for rank, item in enumerate(ranked[:k]) if item in relevant
    )


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    ideal = math.fsum(1.0 / math.log2(rank + 2) for rank in range(min(len(relevant), k)))
    return dcg_at_k(ranked, relevant, k) / ideal if ideal else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for rank, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def average_precision(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    hits = 0
    total = 0.0
    for rank, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            hits += 1
            total += hits / rank
    return total / min(len(relevant), k)


class CalibrationEvaluator:
    """Expected calibration error and Brier score over predicted confidences.

    Calibration is why a confidence field is worth having at all: a model that says
    0.9 should be right about 90% of the time, and one that is confidently wrong is
    more dangerous than one that is uncertain.
    """

    version = 1

    def __init__(
        self,
        *,
        confidence_field: str = "confidence",
        correct_field: str | None = None,
        prediction_field: str = "intent",
        label_field: str = "intent",
        bins: int = 10,
        name: str = "calibration",
    ) -> None:
        self.name = name
        self.confidence_field = confidence_field
        self.correct_field = correct_field
        self.prediction_field = prediction_field
        self.label_field = label_field
        self.bins = bins

    def evaluate_corpus(self, results: Sequence[ExampleResult]) -> list[Metric]:
        points: list[tuple[float, bool]] = []
        for result in results:
            if not result.ok or result.expected is None:
                continue
            confidence = try_resolve(result.output, self.confidence_field)
            if not isinstance(confidence, int | float) or isinstance(confidence, bool):
                continue
            if self.correct_field:
                correct = bool(try_resolve(result.output, self.correct_field))
            else:
                predicted = try_resolve(result.output, self.prediction_field)
                correct = str(predicted) == str(result.expected.get(self.label_field))
            points.append((float(confidence), correct))

        if not points:
            return [Metric(key=f"{self.name}_ece", value=0.0, count=0)]

        return [
            Metric(
                key=f"{self.name}_ece",
                value=expected_calibration_error(points, self.bins),
                count=len(points),
            ),
            Metric(key=f"{self.name}_brier", value=brier_score(points), count=len(points)),
        ]


def expected_calibration_error(points: Sequence[tuple[float, bool]], bins: int = 10) -> float:
    """Weighted mean gap between confidence and accuracy across equal-width bins."""
    if not points:
        return 0.0
    buckets: dict[int, list[tuple[float, bool]]] = defaultdict(list)
    for confidence, correct in points:
        index = min(bins - 1, max(0, int(confidence * bins)))
        buckets[index].append((confidence, correct))

    total = len(points)
    return math.fsum(
        (len(bucket) / total)
        * abs(
            mean([1.0 if correct else 0.0 for _, correct in bucket]) - mean([c for c, _ in bucket])
        )
        for bucket in buckets.values()
    )


def brier_score(points: Sequence[tuple[float, bool]]) -> float:
    if not points:
        return 0.0
    return mean([(confidence - (1.0 if correct else 0.0)) ** 2 for confidence, correct in points])


class DiscriminationEvaluator:
    """ROC-AUC over a predicted score against a binary outcome.

    Exists because "does this model rank the right cases higher?" is an ordinary supervised
    learning question with an ordinary answer, and it is the place teams most often reach for
    an LLM judge when they should be doing ordinary ML evaluation. Asking a model whether a
    mastery prediction was good is strictly worse than measuring it against the next answer
    the learner actually gave.

    AUC rather than accuracy, because the useful property is *ranking*: a mastery predictor
    that outputs 0.4 for everyone who fails and 0.6 for everyone who passes is perfectly
    discriminating and 0% accurate at a 0.5 threshold.
    """

    version = 1

    def __init__(
        self,
        *,
        score_field: str = "predicted",
        outcome_field: str = "correct",
        name: str = "discrimination",
    ) -> None:
        self.name = name
        self.score_field = score_field
        self.outcome_field = outcome_field

    def evaluate_corpus(self, results: Sequence[ExampleResult]) -> list[Metric]:
        points: list[tuple[float, bool]] = []
        for result in results:
            if not result.ok or result.expected is None:
                continue
            score = try_resolve(result.output, self.score_field)
            if not isinstance(score, int | float) or isinstance(score, bool):
                continue
            outcome = result.expected.get(self.outcome_field)
            if outcome is None:
                continue
            points.append((float(score), bool(outcome)))

        value, reason = roc_auc(points)
        if value is None:
            # Zero measurements, not 0.5. An AUC of 0.5 means "no better than chance", which
            # is a measured result; a set with only one class has no AUC at all, and reporting
            # chance-level there would look like a real finding.
            return [Metric(key=f"{self.name}_auc", value=0.0, count=0, unit=reason)]
        return [Metric(key=f"{self.name}_auc", value=value, count=len(points))]


def roc_auc(points: Sequence[tuple[float, bool]]) -> tuple[float | None, str]:
    """ROC-AUC by the rank-sum identity, returning `(value, reason_if_undefined)`.

    Computed as the Mann-Whitney U statistic over midranks rather than by integrating a
    trapezoid over thresholds. The two agree exactly, and the rank form handles ties
    correctly without any special-casing — which matters here because a mastery predictor
    emitting a handful of discrete probabilities produces ties constantly.

    Undefined when either class is empty: AUC asks "how often does a positive outrank a
    negative", and with no negatives there is nothing to outrank.
    """
    positives = [score for score, outcome in points if outcome]
    negatives = [score for score, outcome in points if not outcome]
    if not positives or not negatives:
        return None, "auc undefined: only one outcome class present"

    ordered = sorted(points, key=lambda item: item[0])
    ranks: list[float] = [0.0] * len(ordered)
    index = 0
    while index < len(ordered):
        stop = index
        while stop + 1 < len(ordered) and ordered[stop + 1][0] == ordered[index][0]:
            stop += 1
        # Midrank for a tied group, which is what makes a tie count as half a win.
        shared = (index + stop) / 2 + 1
        for position in range(index, stop + 1):
            ranks[position] = shared
        index = stop + 1

    positive_rank_sum = math.fsum(
        rank for rank, (_, outcome) in zip(ranks, ordered, strict=True) if outcome
    )
    n_pos, n_neg = len(positives), len(negatives)
    statistic = positive_rank_sum - n_pos * (n_pos + 1) / 2
    return statistic / (n_pos * n_neg), ""
