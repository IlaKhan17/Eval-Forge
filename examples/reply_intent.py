"""A minimal reply-intent classifier, used by evals/suites/reply-intent.yaml.

Deliberately rule-based rather than model-backed: the suite's job here is to
exercise Proofstep, and a deterministic task keeps the example reproducible and
free to run.
"""

from __future__ import annotations

import os
from typing import Any

RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("remove me", "stop emailing", "unsubscribe"), "unsubscribe"),
    (("meet", "calendar", "tuesday"), "meeting_requested"),
    (("not interested", "no thanks"), "not_interested"),
    (("pricing", "tell me more", "more about"), "needs_information"),
    (("out of office", "on leave"), "out_of_office"),
)


def _break_unsubscribe() -> bool:
    """Lets a test simulate the regression the protected gate exists to catch.

    Read per call, not at import. A module-level constant is fixed the moment the
    module is first imported, so anything that changes the environment afterwards —
    a test, a re-run in the same process — is silently ignored.
    """
    return os.environ.get("EXAMPLE_BREAK_UNSUBSCRIBE") == "1"


async def classify(example: Any) -> dict[str, Any]:
    body = str(example.input.get("body", "")).lower()
    for needles, intent in RULES:
        if any(needle in body for needle in needles):
            if intent == "unsubscribe" and _break_unsubscribe():
                return {"intent": "needs_information", "confidence": 0.62}
            return {"intent": intent, "confidence": 0.91}
    return {"intent": "ambiguous", "confidence": 0.40}
