"""Turning decoded OTLP spans into an `IngestBatch`.

A translation layer, not a second ingestion path. It builds exactly the same `IngestBatch`
the native endpoint accepts and hands it to the same `IngestService`, so OTLP traffic gets
redaction, payload offloading, idempotent upserts, and rollup recomputation for free — and
cannot drift from the native path, because there is nothing to drift.

**OTLP has no concept of a trace.** It carries spans, and a trace is whatever set of spans
share a trace id. Everything trace-level that EvalForge stores — name, session, environment,
status — has to be inferred, and the inference is the interesting part of this file:

- the **name** comes from the root span, when one is in the batch. A batch of child spans
  cannot name the trace, so it declares nothing and the ingest service stubs the trace from
  its spans; the name arrives when the root does.
- the **session and user** come from whichever span carries them, because OpenInference puts
  them on spans rather than on the resource.
- the **environment and commit** come from resource attributes.

`dropped_span_count` is deliberately left at zero. OTLP has no field for it: the SDK's
`BatchSpanProcessor` drops spans silently and the Collector reports its drops in its own
metrics, so any number here would be invented. A trace that lost spans over OTLP therefore
looks complete, which is worth knowing when reading a trajectory verdict — and is why the
native SDK path reports it explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from evalforge_api.api.schemas.ingest import (
    MAX_SPANS_PER_BATCH,
    IngestBatch,
    ResourceIn,
    SpanEventIn,
    SpanIn,
    TokenUsageIn,
    TraceIn,
)
from evalforge_api.otlp.decode import DecodedScope, DecodedSpan
from evalforge_api.otlp.mapping import map_span, status_for, trace_fields

#: OTLP ids are 16 and 8 bytes, so 32 and 16 hex characters. Anything else is a client bug.
_TRACE_ID_LEN = 32
_SPAN_ID_LEN = 16


@dataclass(slots=True)
class Translation:
    batch: IngestBatch
    #: Spans that could not be translated, with the reason. Reported to the client through
    #: OTLP's `partial_success`, so a broken exporter learns what is wrong instead of
    #: retrying forever against a 200.
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def rejection_summary(self) -> str:
        if not self.rejected:
            return ""
        reasons: dict[str, int] = {}
        for _, reason in self.rejected:
            reasons[reason] = reasons.get(reason, 0) + 1
        return "; ".join(f"{count} span(s): {reason}" for reason, count in sorted(reasons.items()))


def translate(scopes: list[DecodedScope]) -> Translation:
    """Build one `IngestBatch` from every scope in a request."""
    spans: list[SpanIn] = []
    rejected: list[tuple[str, str]] = []
    resource: dict[str, Any] = {}
    #: trace id -> the root span that names it, plus the extents seen for it.
    roots: dict[str, DecodedSpan] = {}
    extents: dict[str, tuple[datetime, datetime | None]] = {}
    identity: dict[str, tuple[str | None, str | None]] = {}

    for scope in scopes:
        # Resources are merged across scopes rather than tracked per span. A single OTLP
        # request from one process carries one resource in practice, and `IngestBatch` has
        # one — splitting a request per resource would multiply the round trips for a case
        # that does not occur.
        resource.update(scope.resource)

        for decoded in scope.spans:
            if len(spans) >= MAX_SPANS_PER_BATCH:
                rejected.append((decoded.span_id, "batch span limit exceeded"))
                continue

            problem = _validate(decoded)
            if problem is not None:
                rejected.append((decoded.span_id or "<no id>", problem))
                continue

            mapped = map_span(decoded.attributes, span_name=decoded.name)
            spans.append(_to_span_in(decoded, mapped, scope))

            if decoded.parent_span_id is None:
                roots[decoded.trace_id] = decoded
            start, end = extents.get(decoded.trace_id, (decoded.start, decoded.end))
            extents[decoded.trace_id] = (
                min(start, decoded.start),
                _later(end, decoded.end),
            )
            if mapped.session_id or mapped.user_ref:
                known_session, known_user = identity.get(decoded.trace_id, (None, None))
                identity[decoded.trace_id] = (
                    known_session or mapped.session_id,
                    known_user or mapped.user_ref,
                )

    fields = trace_fields(resource)
    traces = [
        _to_trace_in(root, extents.get(trace_id), identity.get(trace_id, (None, None)), fields)
        for trace_id, root in roots.items()
    ]

    return Translation(
        batch=IngestBatch(
            resource=ResourceIn(
                **{
                    "service.name": fields["service_name"],
                    "environment": fields["environment"],
                    "git.commit": fields["git_commit"],
                    "sdk.name": _scope_name(scopes),
                    "sdk.version": _scope_version(scopes),
                }
            ),
            traces=traces,
            spans=spans,
        ),
        rejected=rejected,
    )


def _validate(span: DecodedSpan) -> str | None:
    """Why this span cannot be stored, or None.

    Ids are checked because everything downstream keys on them: a malformed trace id would
    create an unreachable trace, and a span with no id cannot be deduplicated on replay.
    Rejecting the span rather than the request keeps one broken span from discarding the
    valid ones around it.
    """
    if len(span.trace_id) != _TRACE_ID_LEN or not _is_hex(span.trace_id):
        return f"trace_id must be {_TRACE_ID_LEN} hex characters"
    if len(span.span_id) != _SPAN_ID_LEN or not _is_hex(span.span_id):
        return f"span_id must be {_SPAN_ID_LEN} hex characters"
    if span.parent_span_id is not None and (
        len(span.parent_span_id) != _SPAN_ID_LEN or not _is_hex(span.parent_span_id)
    ):
        return f"parent_span_id must be {_SPAN_ID_LEN} hex characters"
    if set(span.trace_id) == {"0"} or set(span.span_id) == {"0"}:
        # The all-zero id is OTLP's "invalid" sentinel; storing it would merge every
        # broken exporter's spans into one nonsense trace.
        return "all-zero ids are invalid"
    return None


def _is_hex(value: str) -> bool:
    return all(character in "0123456789abcdefABCDEF" for character in value)


def _to_span_in(decoded: DecodedSpan, mapped: Any, scope: DecodedScope) -> SpanIn:
    attributes = dict(mapped.attributes)
    if scope.scope_name:
        # Kept, because "which instrumentation produced this?" is the first question when a
        # span is mapped oddly, and OTLP puts the answer on the scope rather than the span.
        attributes["otel.scope.name"] = scope.scope_name
    if scope.scope_version:
        attributes["otel.scope.version"] = scope.scope_version
    if decoded.kind and decoded.kind != "SPAN_KIND_UNSPECIFIED":
        attributes["otel.span.kind"] = decoded.kind
    if decoded.dropped_attributes:
        # Surfaced rather than ignored: a span whose attributes were truncated upstream may
        # be missing its model or token counts, and zero tokens with no explanation reads as
        # a free call.
        attributes["otel.dropped_attributes_count"] = decoded.dropped_attributes
    if decoded.dropped_events:
        attributes["otel.dropped_events_count"] = decoded.dropped_events

    status = status_for(decoded.status_code, has_exception=decoded.has_exception)
    return SpanIn(
        trace_id=decoded.trace_id,
        span_id=decoded.span_id,
        parent_span_id=decoded.parent_span_id,
        name=decoded.name[:200],
        span_type=mapped.span_type,
        status=status,
        status_message=decoded.status_message or decoded.exception_message,
        started_at=decoded.start,
        ended_at=decoded.end,
        attributes=attributes,
        input=mapped.input,
        output=mapped.output,
        tool_args=mapped.tool_args,
        events=[
            SpanEventIn(
                name=event.name[:200], timestamp=event.timestamp, attributes=event.attributes
            )
            for event in decoded.events
        ],
        model=mapped.model,
        provider=mapped.provider,
        tokens=(
            TokenUsageIn(
                prompt=mapped.prompt_tokens,
                completion=mapped.completion_tokens,
                total=mapped.total_tokens,
            )
            if mapped.total_tokens
            else None
        ),
        cost=mapped.cost,
        tool_name=mapped.tool_name,
        # An exception event names the failure; OTLP's status message often does not.
        error_type=decoded.exception_type if status == "error" else None,
    )


def _to_trace_in(
    root: DecodedSpan,
    extent: tuple[datetime, datetime | None] | None,
    identity: tuple[str | None, str | None],
    fields: dict[str, Any],
) -> TraceIn:
    start, end = extent or (root.start, root.end)
    session_id, user_ref = identity
    return TraceIn(
        trace_id=root.trace_id,
        name=root.name[:200],
        status=status_for(root.status_code, has_exception=root.has_exception),
        # The extent across every span in the batch, not the root's own times. A child that
        # outlives its parent is normal — a fire-and-forget task closes after the span that
        # started it — and clipping the trace to the root would hide it.
        started_at=min(start, root.start),
        ended_at=_later(end, root.end),
        environment=fields["environment"],
        git_commit=fields["git_commit"],
        session_id=session_id,
        user_ref=user_ref,
        # `redacted`, matching the schema default. An OTLP client has no way to declare a
        # capture mode, and assuming `full` for traffic whose provenance we do not control
        # would opt someone into storing raw prompts without asking.
        capture_mode="redacted",
        metadata={"otlp": True, "service.name": fields["service_name"]}
        if fields["service_name"]
        else {"otlp": True},
    )


def _later(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _scope_name(scopes: list[DecodedScope]) -> str | None:
    for scope in scopes:
        if scope.scope_name:
            return scope.scope_name[:100]
    return None


def _scope_version(scopes: list[DecodedScope]) -> str | None:
    for scope in scopes:
        if scope.scope_version:
            return scope.scope_version[:50]
    return None


__all__ = ["Translation", "translate"]
