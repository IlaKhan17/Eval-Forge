"""Adaptive learning — ordinary supervised evaluation, not an eval-with-a-judge.

This is the section where teams most often reach for an LLM judge when they should be doing
ordinary ML evaluation. "Did the mastery model predict well?" is answered by holding out the
learner's next answer and measuring AUC against it — a standard, cheap, exactly reproducible
metric. Asking a model whether a probability was a good probability is strictly worse.

AUC rather than accuracy, because the useful property is *ranking*: a predictor that outputs
0.4 for everyone who fails and 0.6 for everyone who passes discriminates perfectly and scores
0% accurate at a 0.5 threshold. Brier and calibration error cover whether the numbers mean what
they say.

Only `next_question_relevance` needs a judge, and it is the least consequential metric here.

`QUIZ_BREAK_MASTERY=1` makes the predictor ignore the learner's history, which collapses AUC
while barely moving mean predicted mastery — the reason the gate is on AUC.
"""

from __future__ import annotations

import os
from typing import Any

#: Beta prior on mastery. (2, 2) is weakly informative: it pulls a learner with three answers
#: toward 0.5 rather than letting a single correct answer certify mastery, and it washes out by
#: the time there are twenty. An exponential moving average was the first attempt and is worse
#: here — with four to nine observations it is dominated by whichever answer came last, which
#: showed up immediately as poor AUC and a Brier score at the all-0.5 baseline.
PRIOR_SUCCESSES = 2.0
PRIOR_FAILURES = 2.0


def predict(session: dict[str, Any], *, ignore_history: bool = False) -> dict[str, Any]:
    history: list[dict[str, Any]] = session.get("history") or []
    concept = str(session.get("concept", "unknown"))

    # Difficulty-weighted evidence: getting a hard item right is stronger evidence of mastery
    # than getting an easy one right, and getting an easy one wrong is stronger evidence against.
    successes = PRIOR_SUCCESSES
    failures = PRIOR_FAILURES
    if not ignore_history:
        for answer in history:
            difficulty = float(answer.get("difficulty", 0.5))
            if answer.get("correct"):
                successes += 0.5 + difficulty
            else:
                failures += 1.5 - difficulty

    mastery = round(successes / (successes + failures), 4)

    prerequisites = [
        str(item)
        for item in session.get("candidate_prerequisites") or []
        if _looks_prerequisite(item, concept)
    ]
    misconception = _classify_misconception(history)

    return {
        "concept": concept,
        "predicted_mastery": mastery,
        # The AUC evaluator reads this against the held-out next answer's correctness.
        "predicted": mastery,
        # The point prediction the probability implies, for the calibration evaluator. Reported
        # separately from the probability because calibration asks whether a stated 0.8 is right
        # 80% of the time — which needs both the number and the decision it implies.
        "predicted_correct": mastery >= 0.5,
        # Confidence in the *decision*, not the probability of success — which is what the
        # calibration evaluator's contract expects, and they are not the same number. For a
        # learner at mastery 0.2 the model is 80% confident they will get it wrong, and scoring
        # that as 20% confidence would penalise a correct, confident rejection.
        "confidence": round(max(mastery, 1.0 - mastery), 4),
        "prerequisites": prerequisites,
        "misconception": misconception,
        "next_question_difficulty": round(min(0.95, max(0.15, mastery)), 4),
        # What the judge sees for `next_question_relevance`: the concept and the item chosen.
        "next_question": f"A {_band(mastery)} item on {concept}",
    }


def _band(mastery: float) -> str:
    if mastery < 0.4:
        return "foundational"
    if mastery < 0.75:
        return "intermediate"
    return "advanced"


def _looks_prerequisite(candidate: Any, concept: str) -> bool:
    """A deliberately simple prerequisite heuristic.

    Structural rather than semantic: a candidate is a prerequisite if the concept name contains
    it, which is how most taxonomies are actually named ("derivatives" precedes
    "partial derivatives"). The metric measures this against an expert graph, so a weak
    heuristic produces a low score honestly rather than a good score by accident.
    """
    text = str(candidate).lower()
    return text != concept.lower() and text in concept.lower()


def _classify_misconception(history: list[dict[str, Any]]) -> str | None:
    wrong = [answer for answer in history if not answer.get("correct")]
    if not wrong:
        return None
    tags = [str(answer.get("error_tag")) for answer in wrong if answer.get("error_tag")]
    if not tags:
        return "unclassified"
    # The most frequent error tag, ties broken alphabetically so the output is stable across
    # runs on identical data.
    return max(sorted(set(tags)), key=tags.count)


async def predict_mastery(example: Any) -> dict[str, Any]:
    return predict(example.input, ignore_history=os.environ.get("QUIZ_BREAK_MASTERY") == "1")
