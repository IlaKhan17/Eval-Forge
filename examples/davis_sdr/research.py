"""Prospect research — the suite that shows where the deterministic/judge line falls.

The split is the whole point:

- **Does a citation exist, and does its URL resolve to a document in the retrieved corpus?**
  Deterministic, free, exact. Running a judge on this would be waste.
- **Does the cited source actually support the claim it is attached to?** Irreducibly
  semantic. Running a regex on this is impossible.

Getting that boundary right is worth more than any individual metric: a suite that spends a
judge on citation *existence* is paying for something a string comparison does better, and one
that uses a regex for citation *support* is not measuring anything.

`DAVIS_BREAK_CITATIONS=1` drops the source from one claim per prospect, which the deterministic
`citation_present` gate catches for free.

`DAVIS_BREAK_GROUNDING=1` keeps the citation but makes the claim say something the source does
not — the failure only a judge can see, and the reason this suite has one.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

#: Claim templates the researcher can produce, and where each is grounded. A real
#: implementation would extract these from retrieved documents; the shape is what matters.
_CLAIM_KINDS = ("funding", "headcount", "tech_stack", "expansion")


def research(
    example_input: dict[str, Any], *, drop_citation: bool = False, break_grounding: bool = False
) -> dict[str, Any]:
    prospect = example_input.get("prospect") or {}
    corpus = {doc["id"]: doc for doc in example_input.get("corpus") or []}

    claims: list[dict[str, Any]] = []
    for index, raw in enumerate(example_input.get("facts") or []):
        source_id = raw.get("source_id")
        supported = bool(raw.get("supported", True))
        text = str(raw["text"])
        if break_grounding and index == len(example_input.get("facts") or []) - 1:
            # Cited, resolvable, and wrong. Every deterministic check still passes, which is
            # precisely why this metric is not a deterministic check.
            text = "The company is opening an office in Lisbon."
            supported = False
        claim = {
            "kind": raw.get("kind", _CLAIM_KINDS[index % len(_CLAIM_KINDS)]),
            "text": text,
            # The first claim of each prospect loses its citation under the flag, which is the
            # single most common real failure: a model asserting something with no source.
            "source_id": None if (drop_citation and index == 0) else source_id,
            "supported_by_source": supported,
        }
        claims.append(claim)

    cited = [claim for claim in claims if claim["source_id"]]
    resolvable = [claim for claim in cited if claim["source_id"] in corpus]
    freshness = [
        _age_days(corpus[claim["source_id"]].get("published"))
        for claim in resolvable
        if corpus[claim["source_id"]].get("published")
    ]

    return {
        "person_id": prospect.get("person_id"),
        "company_id": prospect.get("company_id"),
        "claims": claims,
        # Deterministic, and each is a separate metric because each has a different fix: a
        # missing citation is a prompt problem, an unresolvable one is a retrieval problem.
        "citation_present_rate": _ratio(len(cited), len(claims)),
        "citation_resolves_rate": _ratio(len(resolvable), len(cited)) if cited else 1.0,
        "all_claims_cited": len(cited) == len(claims),
        "all_citations_resolve": len(resolvable) == len(cited),
        "source_freshness_days": max(freshness) if freshness else 0,
        # The judge's input. Sent as text so the rubric sees the claim beside its source and
        # nothing else — the same allow-list discipline the judge config enforces.
        "claims_with_sources": [
            {
                "claim": claim["text"],
                "source": corpus.get(claim["source_id"], {}).get("text", ""),
            }
            for claim in claims
        ],
        # What a *human* labelled about support, used as the calibration ground truth. Never
        # read by a gate: a suite that graded the judge against the fixture's own answer key
        # would be measuring nothing.
        "reference_unsupported_count": sum(
            1 for claim in claims if not claim["supported_by_source"]
        ),
    }


def _ratio(part: int, whole: int) -> float:
    return 1.0 if whole == 0 else round(part / whole, 4)


def _age_days(published: str | None) -> int:
    if not published:
        return 0
    try:
        when = date.fromisoformat(published)
    except ValueError:
        return 0
    # Fixed reference date, so the fixture's freshness metric does not drift as the calendar
    # advances and quietly break a p95 gate months after anyone touched the suite.
    return (date(2026, 1, 1) - when).days


async def research_prospect(example: Any) -> dict[str, Any]:
    return research(
        example.input,
        drop_citation=os.environ.get("DAVIS_BREAK_CITATIONS") == "1",
        break_grounding=os.environ.get("DAVIS_BREAK_GROUNDING") == "1",
    )
