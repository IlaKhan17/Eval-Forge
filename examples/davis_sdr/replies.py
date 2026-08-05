"""Reply classification over 12 classes.

Rule-based rather than model-backed, for the same reasons as the rest of the reference
examples: reproducible, free, and every failure mode reachable on demand.

The rules are ordered, and the order matters. `unsubscribe` is checked first because a reply
that says "not interested, and please remove me" is an unsubscribe — the opt-out is the part
with legal consequences, and a classifier that reads the first clause and stops has made the
expensive mistake.

`DAVIS_BREAK_UNSUBSCRIBE=1` reclassifies unsubscribes as `needs_information`, which is the
regression the protected gate exists to catch.
"""

from __future__ import annotations

import os
from typing import Any

#: Ordered. The first match wins, so the most consequential class is checked first.
RULES: tuple[tuple[tuple[str, ...], str, float], ...] = (
    (("remove me", "stop emailing", "unsubscribe", "take me off"), "unsubscribe", 0.96),
    (("not the right person", "want our cto", "wrong person"), "wrong_person", 0.90),
    (("my colleague", "forwarding to", "referral"), "referral", 0.88),
    (("already use you", "existing customer", "already a customer"), "already_a_customer", 0.90),
    (("out of office", "on leave"), "out_of_office", 0.94),
    (("too expensive", "price is high", "pricing objection"), "pricing_objection", 0.86),
    (("next quarter", "not the right time", "check back"), "timing_objection", 0.86),
    (("competitor", "another vendor", "evaluated another"), "competitor_mention", 0.85),
    (("meet", "calendar", "tuesday"), "meeting_requested", 0.91),
    (("not interested", "no thanks", "all set"), "not_interested", 0.89),
    (("pricing", "tell me more", "more about"), "needs_information", 0.87),
)


async def classify_reply(example: Any) -> dict[str, Any]:
    body = str(example.input.get("body", "")).lower()
    broken = os.environ.get("DAVIS_BREAK_UNSUBSCRIBE") == "1"

    for needles, intent, confidence in RULES:
        if any(needle in body for needle in needles):
            if intent == "unsubscribe" and broken:
                return {"intent": "needs_information", "confidence": 0.62}
            return {"intent": intent, "confidence": confidence}
    # Explicitly ambiguous rather than a guess. A reply the classifier cannot place should be
    # routed to a human, and labelling it with a low-confidence guess removes that option.
    return {"intent": "ambiguous", "confidence": 0.38}
