"""W3C trace-context propagation across service boundaries.

Uses the standard `traceparent` header rather than a bespoke one, so a request that
crosses into a service instrumented with plain OpenTelemetry still stitches together.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from evalforge.context import current_span, current_trace

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping

TRACEPARENT = "traceparent"
_FORMAT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def inject(headers: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Add `traceparent` for the current span, if any."""
    out: dict[str, str] = dict(headers or {})
    trace = current_trace()
    span = current_span()
    if trace is None or span is None:
        return out
    flags = "01" if trace.sampled else "00"
    out[TRACEPARENT] = f"00-{trace.trace_id}-{span.span_id}-{flags}"
    return out


def extract(headers: Mapping[str, str]) -> tuple[str, str, bool] | None:
    """Parse `traceparent` into (trace_id, parent_span_id, sampled).

    Returns None on anything malformed. A caller-supplied header is untrusted input
    and must never be able to corrupt our ids.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get(TRACEPARENT)
    if not raw:
        return None
    match = _FORMAT.match(raw.strip())
    if not match:
        return None
    trace_id, span_id, flags = match.groups()
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return trace_id, span_id, bool(int(flags, 16) & 0x01)
