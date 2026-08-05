"""Email quality — six of ten metrics deterministic, and that is the lesson.

Placeholder leakage (`[Your Name]`, `{{first_name}}`) is the single most embarrassing failure
mode in outbound email, and it is a regex. Spending a judge on it would be absurd: slower,
costlier, and less reliable than the three lines below.

The judge is reserved for the residue that genuinely needs one — whether the personalisation
is *grounded* in evidence rather than invented, and whether the tone fits. Those cannot be
pattern-matched, and everything else here can.

`DAVIS_BREAK_PLACEHOLDERS=1` leaves a merge token in the body.
`DAVIS_BREAK_CLAIMS=1` adds a claim outside the approved set.
"""

from __future__ import annotations

import os
import re
from typing import Any

#: Merge tokens from every templating dialect an SDR stack is likely to involve. A single
#: pattern would miss whichever one the next tool uses.
PLACEHOLDER_PATTERNS = (
    r"\[[A-Za-z][A-Za-z0-9 _-]{1,40}\]",  # [Your Name]
    r"\{\{[^}]{1,60}\}\}",  # {{first_name}}
    r"\$\{[^}]{1,60}\}",  # ${company}
    r"<<[^>]{1,60}>>",  # <<Company>>
    r"\bTODO\b|\bFIXME\b|\bXXX\b",
)
_PLACEHOLDER = re.compile("|".join(PLACEHOLDER_PATTERNS))

#: Claims Davis is allowed to make. Anything else is unapproved, whether or not it is true —
#: legal signs off on this list, not on the model.
APPROVED_CLAIMS = frozenset(
    {
        "reduces_manual_entry",
        "integrates_with_crm",
        "soc2_type2",
        "deploys_in_a_week",
        "used_by_similar_teams",
    }
)

SUBJECT_MAX = 78
BODY_MIN, BODY_MAX = 200, 1200


def compose(
    example_input: dict[str, Any],
    *,
    leak_placeholder: bool = False,
    unapproved_claim: bool = False,
) -> dict[str, Any]:
    prospect = example_input.get("prospect") or {}
    evidence = example_input.get("evidence") or []
    first_name = str(prospect.get("first_name") or "there")
    company = str(prospect.get("company") or "your team")

    hook = evidence[0]["text"] if evidence else "your recent work"
    subject = f"{company}: cutting manual CRM entry"[:SUBJECT_MAX]

    body = (
        f"Hi {first_name},\n\n"
        f"Saw that {hook}. Teams at that stage usually lose a few hours a week to manual "
        f"CRM entry, and that is the part we remove.\n\n"
        f"We integrate with your CRM directly and most teams are live in about a week. "
        f"Happy to show you what that looks like on your own pipeline.\n\n"
        f"Would Thursday or Friday suit for fifteen minutes?\n\n"
        f"— Dana"
    )
    if leak_placeholder:
        body = body.replace(f"Hi {first_name}", "Hi [First Name]")
    # Padded to clear the length floor without changing what the checks see. Real emails vary;
    # the fixture should not fail a length gate for reasons unrelated to the thing under test.
    while len(body) < BODY_MIN:
        body += "\nHappy to send over a short summary first if that is easier."

    claims = ["reduces_manual_entry", "integrates_with_crm", "deploys_in_a_week"]
    if unapproved_claim:
        claims.append("guarantees_3x_pipeline")

    return {
        "subject": subject,
        "body": body,
        "claims": claims,
        # Deterministic checks, each a separate metric because each has its own fix.
        "has_placeholder": bool(_PLACEHOLDER.search(subject) or _PLACEHOLDER.search(body)),
        "no_placeholders": not bool(_PLACEHOLDER.search(subject) or _PLACEHOLDER.search(body)),
        "subject_length_ok": 10 <= len(subject) <= SUBJECT_MAX,
        "body_length_ok": BODY_MIN <= len(body) <= BODY_MAX,
        "claims_approved": set(claims).issubset(APPROVED_CLAIMS),
        "unapproved_claims": sorted(set(claims) - APPROVED_CLAIMS),
        # What the judge sees: the body beside the evidence it is supposed to be grounded in.
        # Never the expected output — a judge handed the answer key grades itself.
        "evidence_text": "\n".join(item["text"] for item in evidence),
    }


async def compose_email(example: Any) -> dict[str, Any]:
    return compose(
        example.input,
        leak_placeholder=os.environ.get("DAVIS_BREAK_PLACEHOLDERS") == "1",
        unapproved_claim=os.environ.get("DAVIS_BREAK_CLAIMS") == "1",
    )
