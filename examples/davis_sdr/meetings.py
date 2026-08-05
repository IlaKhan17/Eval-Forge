"""Meeting intelligence — dates and owners are deterministic, prose is not.

A date either parses to the right day or it does not. An owner either is or is not in the
attendee list. Both are exact comparisons against ground truth, and spending a judge on either
would be slower and *less* accurate than the comparison.

Only the factuality of the generated prose needs a judge, and it is gated hardest — a summary
that invents a commitment is worse than one that misses a date.

`DAVIS_BREAK_DATES=1` shifts extracted dates by a day, which the deterministic gate catches.
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Any

#: Relative expressions a transcript actually contains, resolved against the meeting date.
_RELATIVE = {
    "today": 0,
    "tomorrow": 1,
    "next week": 7,
    "in two weeks": 14,
    "next month": 30,
}
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def extract(example_input: dict[str, Any], *, shift_dates: bool = False) -> dict[str, Any]:
    transcript = str(example_input.get("transcript", ""))
    attendees = [str(name) for name in example_input.get("attendees") or []]
    meeting_date = _parse_date(example_input.get("meeting_date")) or date(2026, 1, 15)
    competitors = {name.lower() for name in example_input.get("known_competitors") or []}

    action_items: list[dict[str, Any]] = []
    for line in transcript.splitlines():
        if not line.strip().startswith("ACTION:"):
            continue
        body = line.split("ACTION:", 1)[1].strip()
        owner = next((name for name in attendees if name.lower() in body.lower()), None)
        due = _resolve_date(body, meeting_date)
        if due and shift_dates:
            due = due + timedelta(days=1)
        action_items.append(
            {
                "text": body,
                # An owner outside the attendee list is not an owner. Returning None rather
                # than a guess is what makes the attribution metric meaningful — a
                # confidently wrong owner is worse than an unassigned item.
                "owner": owner,
                "due": due.isoformat() if due else None,
            }
        )

    mentioned = sorted(
        name for name in competitors if re.search(rf"\b{re.escape(name)}\b", transcript.lower())
    )
    objections = [
        line.split("OBJECTION:", 1)[1].strip()
        for line in transcript.splitlines()
        if line.strip().startswith("OBJECTION:")
    ]

    return {
        "action_items": [item["text"] for item in action_items],
        "owners": [item["owner"] for item in action_items],
        "dates": [item["due"] for item in action_items],
        "competitors": mentioned,
        "objections": objections,
        "owners_all_attendees": all(
            item["owner"] is None or item["owner"] in attendees for item in action_items
        ),
        "summary": _summarize(action_items, objections, mentioned),
        # The judge compares its summary against this, and against nothing else.
        "transcript": transcript,
    }


def _summarize(
    action_items: list[dict[str, Any]], objections: list[str], competitors: list[str]
) -> str:
    parts = [f"{len(action_items)} action item(s) agreed."]
    if objections:
        parts.append(f"Objections raised: {'; '.join(objections)}.")
    if competitors:
        parts.append(f"Competitors mentioned: {', '.join(competitors)}.")
    for item in action_items:
        owner = item["owner"] or "unassigned"
        due = item["due"] or "no date"
        parts.append(f"{owner} to {item['text']} by {due}.")
    return " ".join(parts)


def _resolve_date(text: str, reference: date) -> date | None:
    if match := _ISO.search(text):
        return _parse_date(match.group(1))
    lowered = text.lower()
    for phrase, offset in _RELATIVE.items():
        if phrase in lowered:
            return reference + timedelta(days=offset)
    return None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


async def extract_meeting(example: Any) -> dict[str, Any]:
    return extract(example.input, shift_dates=os.environ.get("DAVIS_BREAK_DATES") == "1")
