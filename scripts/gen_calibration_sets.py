#!/usr/bin/env python
"""Generate the human-labelled calibration sets the reference judges are measured against.

**In a real project these labels come from people.** That is not a formality: the point of
calibration is to compare a judge against human judgement, and a set labelled by a model makes
the exercise circular. Every suite that ships one records "human review mandatory" against it.

What this script produces is a *fixture* — synthetic examples whose correct label is determined
by construction, so the reference suites have something to calibrate against in CI with no
provider and no annotator. The labels are honest about what they are: derived from the marker the
example was built with, not from a person's opinion.

Each set is balanced and carries a doubly-labelled subset with a few deliberate annotator
disagreements, so the human-ceiling comparison has something real to report. A set where the two
annotators agree perfectly would report a ceiling of 1.0 and teach the opposite of what it is for.

    uv run python scripts/gen_calibration_sets.py
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CALIBRATION = ROOT / "evals" / "calibration"

#: Doubly-labelled examples per set, and how many of those the two annotators disagree on.
DOUBLE_LABELLED = 24
DISAGREEMENTS = 3


def write(name: str, rows: list[dict[str, Any]]) -> None:
    CALIBRATION.mkdir(parents=True, exist_ok=True)
    path = CALIBRATION / name
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    labels: dict[str, int] = {}
    for row in rows:
        labels[row["human_label"]] = labels.get(row["human_label"], 0) + 1
    print(f"{path.relative_to(ROOT)}: {len(rows)} examples  {dict(sorted(labels.items()))}")


def add_second_annotator(rows: list[dict[str, Any]], alternatives: dict[str, str]) -> None:
    """Attach a second annotator to a subset, disagreeing on a few.

    Applied to the examples at the front of the list, which are deliberately the boundary cases.
    That is how a real doubly-labelled subset is chosen — you double-label the hard ones — and it
    is exactly why the ceiling comparison has to be restricted to the same subset rather than set
    against the judge's overall agreement.
    """
    # Balanced across labels, not simply the first N. A single-class subset makes the human
    # ceiling degenerate — both annotators used one label, so chance agreement is 1.0 and kappa is
    # undefined — and the report then claims the judge is "at the ceiling" of a ceiling that was
    # never measured. Taking the boundary cases first *and* spanning the classes is what a real
    # doubly-labelled subset looks like.
    by_label: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_label.setdefault(row["human_label"], []).append(row)

    chosen: list[dict[str, Any]] = []
    per_label = max(1, DOUBLE_LABELLED // max(1, len(by_label)))
    for label in sorted(by_label):
        chosen.extend(by_label[label][:per_label])

    for index, row in enumerate(chosen):
        primary = row["human_label"]
        row["second_human_label"] = (
            alternatives.get(primary, primary) if index < DISAGREEMENTS else primary
        )


def claim_groundedness(rng: random.Random) -> list[dict[str, Any]]:
    """Is the claim supported by its cited source?

    The unsupported examples use the Lisbon-office marker — the same one the research fixture uses
    for its genuinely unsupported claim — so the stub judge behaves consistently across both.

    Note the shape of `output`: an object whose keys are exactly the fields the judge's `inputs`
    allow-list names. A calibration set has to present the judge with the same view it gets during
    a run, or every call resolves nothing and errors — which reads as "0 usable labelled examples"
    rather than as a shape mismatch.
    """
    rows = []
    companies = ["Northwind", "Globex", "Initech", "Umbrella", "Stark"]
    for index in range(60):
        company = companies[index % len(companies)]
        rows.append(
            {
                "id": f"ground-s{index:03d}",
                "input": {},
                "output": {
                    "claims_with_sources": [
                        {
                            "claim": f"{company} raised a Series B in 2025.",
                            "source": (
                                f"{company} announced a Series B in 2025 and grew to 240 staff."
                            ),
                        }
                    ]
                },
                "human_label": "supported",
            }
        )
    for index in range(60):
        company = companies[index % len(companies)]
        rows.append(
            {
                "id": f"ground-u{index:03d}",
                "input": {},
                "output": {
                    "claims_with_sources": [
                        {
                            "claim": f"{company} is opening an office in Lisbon.",
                            "source": (
                                f"{company} announced a Series B in 2025 and grew to 240 staff."
                            ),
                        }
                    ]
                },
                "human_label": "unsupported",
            }
        )
    rng.shuffle(rows)
    # Boundary cases first, so the doubly-labelled subset is the hard one.
    rows.sort(key=lambda row: 0 if "Lisbon" in json.dumps(row["output"]) else 1)
    add_second_annotator(rows, {"supported": "unsupported", "unsupported": "supported"})
    return rows


def citation_support(rng: random.Random) -> list[dict[str, Any]]:
    rows = []
    topics = ["derivatives", "limits", "integrals", "series", "vectors"]
    for index in range(60):
        topic = topics[index % len(topics)]
        rows.append(
            {
                "id": f"cite-s{index:03d}",
                "input": {},
                "output": {
                    "question_text": f"Which statement about {topic} is correct?",
                    "keyed_answer": f"{topic.capitalize()} describe how a function changes.",
                    "citation_text": f"The {topic} of a function describe how it changes.",
                },
                "human_label": "supported",
            }
        )
    for index in range(60):
        topic = topics[index % len(topics)]
        rows.append(
            {
                "id": f"cite-u{index:03d}",
                "input": {},
                "output": {
                    "question_text": f"Which statement about {topic} is correct?",
                    "keyed_answer": f"{topic.capitalize()} are always constant.",
                    "citation_text": f"The {topic} of a function describe how it changes.",
                },
                "human_label": "unsupported",
            }
        )
    rng.shuffle(rows)
    rows.sort(key=lambda row: 0 if "always constant" in json.dumps(row["output"]) else 1)
    add_second_annotator(rows, {"supported": "unsupported", "unsupported": "supported"})
    return rows


def rubric_set(
    name: str, *, good: dict[str, Any], bad: dict[str, Any], per_point: int = 24
) -> list[dict[str, Any]]:
    """A 1-5 rubric calibration set, with every rubric point represented.

    Twenty-four per point rather than fifty. Weighted kappa measures *distance* on the scale, so
    it already accounts for near misses; labelling fifty examples of each of five points would
    cost 250 annotations to sharpen a statistic that is not per-class. The suites that use these
    sets lower `min_per_class` to match, with the reasoning recorded there.
    """
    rows = []
    for point in ("5", "4", "3", "2", "1"):
        for index in range(per_point):
            is_good = point in ("4", "5")
            rows.append(
                {
                    "id": f"{name}-{point}-{index:03d}",
                    "input": {},
                    "output": good if is_good else bad,
                    "human_label": point,
                }
            )
    # The 3s are the decision boundary, so they lead the doubly-labelled subset.
    rows.sort(key=lambda row: 0 if row["human_label"] == "3" else 1)
    add_second_annotator(rows, {"3": "4", "4": "3", "2": "3", "5": "4", "1": "2"})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260804)
    args = parser.parse_args()
    # Seeded shuffling for a fixture, not a security decision.
    rng = random.Random(args.seed)  # noqa: S311

    write("claim-groundedness.jsonl", claim_groundedness(rng))
    write("citation-support.jsonl", citation_support(rng))
    write(
        "grounded-personalization.jsonl",
        rubric_set(
            "personalization",
            good={
                "body": (
                    "Saw that Northwind is hiring three revenue-ops roles, and that is exactly "
                    "where manual CRM entry costs the most."
                ),
                "evidence_text": "Northwind is hiring three revenue-ops roles.",
            },
            # Uses the placeholder marker, which the stub reads as bad — and is a real grounding
            # failure too: a body that never resolved its merge field is not personalised at all.
            bad={
                "body": "Hi [First Name], we help teams like yours.",
                "evidence_text": "Northwind is hiring three revenue-ops roles.",
            },
        ),
    )
    write(
        "summary-factuality.jsonl",
        rubric_set(
            "factuality",
            good={
                "summary": "Ada to send the security packet by 2026-02-10.",
                "transcript": "ACTION: Ada to send the security packet by 2026-02-10.",
            },
            bad={
                "summary": "unassigned to send the security packet by no date.",
                "transcript": "ACTION: Ada to send the security packet by 2026-02-10.",
            },
        ),
    )
    write(
        "next-question-relevance.jsonl",
        rubric_set(
            "relevance",
            good={
                "concept": "partial derivatives",
                "predicted_mastery": 0.62,
                "next_question": "An intermediate item on partial derivatives",
            },
            bad={
                "concept": "partial derivatives",
                "predicted_mastery": 0.62,
                "next_question": "An advanced item on vectors, which are always constant",
            },
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
