"""Document ingestion — the clearest case in the project of where evals are the wrong tool.

Every metric here is deterministic, because this is a **parsing** problem with ground truth,
not a subjective one. An LLM judge would be slower, costlier, and less accurate than a string
comparison against an annotated reference.

Several of these are genuinely just unit tests over a fixture corpus, and the suite says so.
They are co-located here because they are cheap to run alongside the rest and their *trend*
over time is informative — a text-extraction coverage that drifts from 0.99 to 0.96 after a
library upgrade is worth a gate. But if this were the only thing being measured, it would
belong in the application's own `pytest` suite and not in an evaluation platform at all.

`QUIZ_BREAK_OFFSETS=1` shifts citation offsets, which the blocking location gate catches.
"""

from __future__ import annotations

import os
import re
from typing import Any

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_EQUATION = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")

#: How far a reported citation offset may be from the reference before it counts as wrong.
#: Non-zero because extractors legitimately differ on leading whitespace; small because a
#: citation that points at the wrong paragraph is useless.
OFFSET_TOLERANCE = 8


def extract(document: dict[str, Any], *, shift_offsets: bool = False) -> dict[str, Any]:
    pages: list[dict[str, Any]] = document.get("pages") or []

    text_parts: list[str] = []
    headings: list[str] = []
    equations: list[str] = []
    tables: list[list[str]] = []
    citations: list[dict[str, Any]] = []
    split_sentences = 0

    for page in pages:
        raw = str(page.get("text", ""))
        text_parts.append(raw)

        for line in raw.splitlines():
            if match := _HEADING.match(line):
                headings.append(match.group(2).strip())
            if row := _TABLE_ROW.match(line):
                cells = [cell.strip() for cell in row.group(1).split("|")]
                tables.append([cell for cell in cells if cell and not set(cell) <= {"-", ":"}])

        equations.extend(_normalize_latex(found) for found in _EQUATION.findall(raw))

        for anchor in page.get("anchors") or []:
            offset = int(anchor["offset"])
            citations.append(
                {
                    "id": anchor["id"],
                    "page": int(page["number"]),
                    # A citation whose offset is wrong points a learner at the wrong sentence,
                    # which is why this gate blocks rather than warns.
                    "offset": offset + (OFFSET_TOLERANCE + 5 if shift_offsets else 0),
                }
            )

        # A sentence split across a section boundary means the extractor broke mid-thought, and
        # everything downstream — chunking, citation offsets, question generation — inherits it.
        if raw.rstrip() and not raw.rstrip().endswith((".", "?", "!", ":", "|", "$")):
            split_sentences += 1

    text = "\n".join(text_parts)
    reference = str(document.get("reference_text", ""))

    return {
        "text": text,
        "char_count": len(text),
        # Coverage against an annotated reference, capped at 1.0: extracting *more* than the
        # reference is not better than extracting it exactly, and letting the ratio exceed one
        # would let a noisy extractor mask a gap elsewhere.
        "text_extraction_coverage": round(min(1.0, len(text) / len(reference)), 4)
        if reference
        else 1.0,
        "headings": headings,
        "equations": equations,
        "table_cells": [cell for row in tables for cell in row],
        "citations": citations,
        "citation_pages": [item["page"] for item in citations],
        "citation_offsets": [item["offset"] for item in citations],
        "cross_page_integrity": round(1.0 - (split_sentences / len(pages)) if pages else 1.0, 4),
    }


def _normalize_latex(equation: str) -> str:
    """Normalize LaTeX enough that equivalent renderings compare equal.

    Whitespace and redundant braces only. Deliberately not a full parser: an extractor that
    emits `\\frac{1}{2}` where the reference says `\\dfrac{1}{2}` is a real difference worth
    surfacing, and normalising it away would hide a regression behind a lenient comparison.
    """
    collapsed = " ".join(equation.split())
    return collapsed.replace("{ ", "{").replace(" }", "}").strip()


def citation_locations_ok(output: dict[str, Any], expected: dict[str, Any]) -> bool:
    reference = {item["id"]: item for item in expected.get("citations") or []}
    for citation in output.get("citations") or []:
        target = reference.get(citation["id"])
        if target is None:
            return False
        if citation["page"] != target["page"]:
            return False
        if abs(citation["offset"] - target["offset"]) > OFFSET_TOLERANCE:
            return False
    return True


async def ingest_document(example: Any) -> dict[str, Any]:
    output = extract(example.input, shift_offsets=os.environ.get("QUIZ_BREAK_OFFSETS") == "1")
    # The location check needs both sides, so it is computed here rather than expressed as a
    # field comparison in the suite. A `custom_python` evaluator would be the alternative and
    # is not implemented yet.
    output["citations_located"] = citation_locations_ok(output, example.expected or {})
    return output
