"""Server-side redaction backstop.

The SDK already redacts before export, and that is the real control: data redacted
in the customer's process never reaches us at all. This pass exists for the client
we do not control — an outdated SDK, a hand-rolled curl, an OTLP exporter with no
redaction of its own.

It reuses the SDK's patterns rather than maintaining a second list. Two deny-lists
that drift apart would mean the backstop silently stops covering what the primary
control covers.
"""

from __future__ import annotations

from typing import Any

from proofstep_core.redaction import RedactionPipeline
from proofstep_types import CaptureMode


def scrub(value: Any) -> tuple[Any, int, int]:
    """Redact secrets from an ingested payload.

    Returns the cleaned value, how many redactions were applied, and how many fields
    were truncated for size. Both counts are reported to the client: redactions mean
    their instrumentation is shipping credentials, and truncations mean data they
    expected to see is not there. Either is worth knowing about explicitly.
    """
    if value is None:
        return None, 0, 0
    pipeline = RedactionPipeline(capture_mode=CaptureMode.REDACTED)
    cleaned = pipeline.apply(value)
    return cleaned, pipeline.count, pipeline.truncated
