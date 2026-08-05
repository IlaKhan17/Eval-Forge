"""Lead ranking — the suite with zero judges.

Ranking quality is measurable against human labels with standard IR metrics. Asking a model
"is this ranking good?" would be slower, cost money, and be less accurate than NDCG against
labels a human assigned — so this suite deliberately contains no judge at all.

`DAVIS_BREAK_RANKING=1` injects the regression the suite exists to catch: a scoring change
that promotes disqualified leads. It moves NDCG a little and the false-positive rate a lot,
which is exactly why the blocking gate is on the latter.
"""

from __future__ import annotations

import os
from typing import Any

#: Signals the scorer weighs, and how much. Deliberately simple and legible: the point of the
#: example is the evaluation, not the model.
_WEIGHTS = {
    "icp_fit": 3.0,
    "recent_funding": 2.0,
    "hiring_signal": 1.5,
    "tech_match": 1.5,
    "engagement": 1.0,
}

#: Reasons a lead is disqualified outright, whatever else it scores.
_DISQUALIFIERS = ("competitor", "existing_customer", "do_not_contact", "out_of_region")


def score_lead(lead: dict[str, Any], *, broken: bool = False) -> tuple[float, list[str]]:
    """A score in [0, 1] plus any disqualification reasons."""
    reasons = [reason for reason in _DISQUALIFIERS if lead.get(reason)]

    raw = sum(weight * float(lead.get(signal, 0)) for signal, weight in _WEIGHTS.items())
    score = raw / sum(_WEIGHTS.values())

    if reasons and not broken:
        # A disqualified lead is not a low-scoring lead; it is not a lead. Floor rather than
        # penalty, so no amount of ICP fit can lift it back into the top ten.
        score = 0.0
    return round(min(1.0, score), 4), reasons


def rank(example_input: dict[str, Any], *, broken: bool = False) -> dict[str, Any]:
    candidates = example_input.get("candidates") or []
    scored = []
    seen_entities: dict[str, str] = {}

    for lead in candidates:
        score, reasons = score_lead(lead, broken=broken)
        # Entity resolution before ranking, so two records for the same company cannot both
        # occupy a slot in the top ten. Doing it after would leave the duplicate visible to a
        # human even though the metric looked clean.
        entity_key = _entity_key(lead)
        canonical = seen_entities.setdefault(entity_key, str(lead["id"]))
        scored.append(
            {
                "id": str(lead["id"]),
                "company_id": entity_key,
                "canonical_id": canonical,
                "score": score,
                "disqualification_reasons": reasons,
                "evidence": lead.get("evidence") or [],
            }
        )

    deduped = [item for item in scored if item["id"] == item["canonical_id"]]
    # Sorted by score, then by id, so an equal-scoring pair does not reorder between runs and
    # produce a different NDCG on identical data.
    deduped.sort(key=lambda item: (-item["score"], item["id"]))

    return {
        "ranked": [item["id"] for item in deduped],
        "results": [item["id"] for item in deduped],
        "scores": {item["id"]: item["score"] for item in deduped},
        "disqualified": {
            item["id"]: item["disqualification_reasons"]
            for item in deduped
            if item["disqualification_reasons"]
        },
        "duplicate_ids": [item["id"] for item in scored if item["id"] != item["canonical_id"]],
        "entities": {item["id"]: item["company_id"] for item in deduped},
        "evidence_count": {item["id"]: len(item["evidence"]) for item in deduped},
        "all_scored_have_evidence": all(item["evidence"] for item in deduped),
        "top_disqualified_count": sum(
            1 for item in deduped[:10] if item["disqualification_reasons"]
        ),
    }


def _entity_key(lead: dict[str, Any]) -> str:
    """Normalize a company to a comparable key.

    Domain first, name second. A name is ambiguous — "Acme", "Acme Inc.", "ACME Corporation"
    — while a domain is the closest thing to a primary key a public company has.
    """
    domain = str(lead.get("domain") or "").strip().lower()
    if domain:
        return domain.removeprefix("www.")
    name = str(lead.get("company") or "").strip().lower()
    for suffix in (" inc.", " inc", " corporation", " corp.", " corp", " ltd", " llc", " gmbh"):
        name = name.removesuffix(suffix)
    return " ".join(name.split())


async def rank_leads(example: Any) -> dict[str, Any]:
    """Task entrypoint.

    The flag is read per call rather than at import, so the same process can run a clean and
    a broken pass — which is how the CI check demonstrates the gate firing.
    """
    return rank(example.input, broken=os.environ.get("DAVIS_BREAK_RANKING") == "1")
