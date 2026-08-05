"""Question generation — where a judge cannot be the sole arbiter of correctness.

A quiz platform that teaches wrong answers is worse than useless, and a judge is *exactly as
likely to be wrong as the generator* and correlated in its errors: both are language models
reasoning over the same passage. So `answer_correctness` is human ground truth, and the judge
is calibrated against it rather than trusted in its place.

Everything mechanical is deterministic and free: schema validity, exactly-one-keyed-answer,
near-duplicate detection, citation presence and resolution. Only three properties genuinely
need a judge — whether the cited passage *supports* the keyed answer, whether the question is
answerable from that passage alone, and whether the distractors are plausible but
unambiguously wrong.

`QUIZ_BREAK_ANSWER_KEY=1` keys two options correct, which the deterministic gate catches.
`QUIZ_BREAK_DEDUP=1` emits a near-duplicate question.
"""

from __future__ import annotations

import os
import re
from typing import Any

_WORD = re.compile(r"[a-z0-9]+")

#: Jaccard similarity above which two questions count as near-duplicates. Token-set based rather
#: than embedding-based: a normalized-text comparison is free, explainable, and catches the failure
#: that actually occurs — a generator re-emitting a question it already produced, modulo casing and
#: whitespace.
#:
#: It does *not* catch a genuine paraphrase, whose token sets differ. Embedding dedup would, and is
#: deferred; loosening this threshold to reach paraphrases would instead start flagging distinct
#: questions on the same topic, which is worse than the gap.
DUPLICATE_THRESHOLD = 0.8


def generate(
    example_input: dict[str, Any],
    *,
    break_key: bool = False,
    force_duplicate: bool = False,
) -> dict[str, Any]:
    passage = example_input.get("passage") or {}
    spec = example_input.get("spec") or {}

    stem = str(
        spec.get("stem") or f"Which statement about {passage.get('topic', 'the topic')} is correct?"
    )
    correct = str(spec.get("correct") or "")
    distractors = [str(item) for item in spec.get("distractors") or []]

    options = [
        {"text": correct, "correct": True},
        *({"text": text, "correct": False} for text in distractors),
    ]
    if break_key and len(options) > 1:
        # Two keyed answers. A quiz with two right answers is unanswerable, and it is a
        # deterministic property — no judge required to notice.
        options[1]["correct"] = True

    questions = [
        {
            "id": str(spec.get("id", "q-1")),
            "stem": stem,
            "options": options,
            "citation": {
                "document_id": passage.get("document_id"),
                "page": passage.get("page"),
                "text": passage.get("text", ""),
            },
            "objective": spec.get("objective"),
            "predicted_difficulty": float(spec.get("predicted_difficulty", 0.5)),
        }
    ]
    if force_duplicate:
        # The same stem with different whitespace and casing — the failure token-set Jaccard is
        # built for, and the one that actually happens: a generator re-emitting a question it
        # already produced.
        #
        # A *paraphrase* ("Which of these statements…") slips past this check, because the token
        # sets genuinely differ. That is a real limitation rather than a threshold to tune, and it
        # is why embedding-based dedup is a listed deferral: catching paraphrases needs a different
        # mechanism, not a looser bar on this one.
        near = dict(questions[0])
        near["id"] = f"{near['id']}-dup"
        near["stem"] = f"  {stem.upper()}  "
        questions.append(near)

    corpus_ids = {doc["id"] for doc in example_input.get("corpus") or []}
    keyed_counts = [sum(1 for option in q["options"] if option["correct"]) for q in questions]

    return {
        "questions": questions,
        "question_count": len(questions),
        # Deterministic, and each is blocking, because each makes the question unusable.
        "schema_valid": all(_schema_ok(question) for question in questions),
        "single_correct_answer": all(count == 1 for count in keyed_counts),
        "citation_present": all(question["citation"]["document_id"] for question in questions),
        "citation_resolves": all(
            question["citation"]["document_id"] in corpus_ids for question in questions
        ),
        "duplicate_rate": _duplicate_rate(questions),
        "grammar_ok": all(_grammar_ok(question["stem"]) for question in questions),
        "predicted_difficulty": questions[0]["predicted_difficulty"],
        "objective": questions[0]["objective"],
        # The judge's view: the question, the keyed answer, and the cited passage. Not the
        # human's correctness label, which would let it grade against the answer key.
        "question_text": stem,
        "keyed_answer": correct,
        "citation_text": str(passage.get("text", "")),
        "distractor_text": " | ".join(distractors),
    }


def _schema_ok(question: dict[str, Any]) -> bool:
    if not question.get("stem") or not question.get("options"):
        return False
    options = question["options"]
    return (
        2 <= len(options) <= 6
        and all(isinstance(option.get("text"), str) and option["text"] for option in options)
        and all(isinstance(option.get("correct"), bool) for option in options)
    )


def _grammar_ok(stem: str) -> bool:
    """A crude, honest grammar check.

    Not a language tool: pulling one in for an example would add a heavyweight dependency to
    demonstrate a metric whose mechanism is not the interesting part. What it does check is the
    failure that actually happens — a stem that is not a well-formed question.
    """
    trimmed = stem.strip()
    return bool(trimmed) and trimmed[0].isupper() and trimmed.endswith(("?", "."))


def _duplicate_rate(questions: list[dict[str, Any]]) -> float:
    if len(questions) < 2:
        return 0.0
    duplicates = 0
    for index, first in enumerate(questions):
        for second in questions[index + 1 :]:
            if _similarity(first["stem"], second["stem"]) >= DUPLICATE_THRESHOLD:
                duplicates += 1
    return round(duplicates / len(questions), 4)


def _similarity(left: str, right: str) -> float:
    a = set(_WORD.findall(left.lower()))
    b = set(_WORD.findall(right.lower()))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


async def generate_question(example: Any) -> dict[str, Any]:
    return generate(
        example.input,
        break_key=os.environ.get("QUIZ_BREAK_ANSWER_KEY") == "1",
        force_duplicate=os.environ.get("QUIZ_BREAK_DEDUP") == "1",
    )
