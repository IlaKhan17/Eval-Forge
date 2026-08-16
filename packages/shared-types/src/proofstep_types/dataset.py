"""Dataset examples — the input side of an evaluation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Example(BaseModel):
    """One evaluation case: an input, an optional expected result, and metadata.

    ``id`` is the *stable external identifier*. Comparison between experiments
    matches on it rather than on position, because datasets gain and lose examples
    between versions and ordinal matching would silently compare unrelated rows
    (docs/EVALUATION_ENGINE.md §6).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    input: dict[str, Any]
    expected: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_trace_id: str | None = None
    source_span_id: str | None = None

    def canonical_json(self) -> str:
        """Deterministic serialization used for dataset content hashing."""
        return json.dumps(
            {
                "id": self.id,
                "input": self.input,
                "expected": self.expected,
                "metadata": self.metadata,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )


def content_hash(examples: list[Example]) -> str:
    """SHA-256 over the canonically serialized examples, in order.

    This is what makes an experiment provably reproducible: the hash is recorded
    alongside the dataset version id, so a later run can prove it used identical
    data rather than merely claiming the same version label (ADR-012).
    """
    digest = hashlib.sha256()
    for example in examples:
        digest.update(example.canonical_json().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
