"""Decoding OTLP/HTTP request bodies into plain Python.

Both encodings the protocol defines are supported, and that is not optional in practice:
every OpenTelemetry SDK and the Collector's `otlphttp` exporter default to
`application/x-protobuf`. A receiver that only spoke JSON would fail the one-line-env-var
promise for almost every real deployment.

The protobuf schema comes from `opentelemetry-proto` rather than a hand-written wire
reader. A varint-level decoder for someone else's stable, external format is exactly where
subtle bugs hide — a misread field number silently files data under the wrong key, and the
symptom appears weeks later as "why is this attribute missing".

The output of this module is plain dicts and dataclasses, so nothing downstream imports
protobuf. That boundary is what lets `mapping.py` be tested against literal attribute
dicts.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.protobuf.json_format import ParseDict
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as PbSpan

PROTOBUF_CONTENT_TYPE = "application/x-protobuf"
JSON_CONTENT_TYPE = "application/json"

#: Nanoseconds. OTLP timestamps are `fixed64` nanos since the Unix epoch.
_NANOS = 1_000_000_000

_STATUS_NAMES = {
    0: "STATUS_CODE_UNSET",
    1: "STATUS_CODE_OK",
    2: "STATUS_CODE_ERROR",
}


class OtlpDecodeError(ValueError):
    """The body is not a decodable OTLP payload."""


@dataclass(slots=True)
class DecodedEvent:
    name: str
    timestamp: datetime
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DecodedSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    start: datetime
    end: datetime | None
    status_code: str
    status_message: str | None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[DecodedEvent] = field(default_factory=list)
    #: Attributes the *producer* dropped before sending. Surfaced because a span whose
    #: attributes were truncated upstream may be missing the model name or token counts, and
    #: silently showing zero tokens is worse than showing that something was lost.
    dropped_attributes: int = 0
    dropped_events: int = 0

    @property
    def has_exception(self) -> bool:
        return any(event.name == "exception" for event in self.events)

    @property
    def exception_type(self) -> str | None:
        for event in self.events:
            if event.name == "exception":
                value = event.attributes.get("exception.type")
                if value:
                    return str(value)[:100]
        return None

    @property
    def exception_message(self) -> str | None:
        for event in self.events:
            if event.name == "exception":
                value = event.attributes.get("exception.message")
                if value:
                    return str(value)[:4000]
        return None


@dataclass(slots=True)
class DecodedScope:
    """One instrumentation scope's spans, with the resource they belong to."""

    resource: dict[str, Any]
    scope_name: str
    scope_version: str
    spans: list[DecodedSpan] = field(default_factory=list)


def decode(body: bytes, content_type: str) -> list[DecodedScope]:
    """Decode a request body, choosing the encoding from the content type.

    An unrecognised content type is treated as protobuf rather than rejected. Collectors
    and hand-rolled clients send `application/x-protobuf`, `application/protobuf`, and
    occasionally nothing at all; failing those would be pedantry with a real cost, and a
    protobuf parse of a JSON body fails loudly anyway.
    """
    normalized = (content_type or "").split(";")[0].strip().lower()
    request = ExportTraceServiceRequest()

    if normalized == JSON_CONTENT_TYPE:
        try:
            payload = json.loads(body or b"{}")
        except (json.JSONDecodeError, ValueError) as exc:
            msg = f"body is not valid JSON: {exc}"
            raise OtlpDecodeError(msg) from exc
        if not isinstance(payload, dict):
            msg = "OTLP/JSON body must be an object"
            raise OtlpDecodeError(msg)
        try:
            # `ignore_unknown_fields`, so a newer collector sending a field this proto
            # version has not learned does not lose the whole batch.
            ParseDict(payload, request, ignore_unknown_fields=True)
        except Exception as exc:
            msg = f"body is not a valid OTLP/JSON ExportTraceServiceRequest: {exc}"
            raise OtlpDecodeError(msg) from exc
    else:
        try:
            request.ParseFromString(body)
        except DecodeError as exc:
            msg = (
                "body is not a valid OTLP protobuf ExportTraceServiceRequest. Send "
                f"content-type: {JSON_CONTENT_TYPE} for the JSON encoding."
            )
            raise OtlpDecodeError(msg) from exc

    return _to_scopes(request)


def _to_scopes(request: ExportTraceServiceRequest) -> list[DecodedScope]:
    scopes: list[DecodedScope] = []
    for resource_spans in request.resource_spans:
        resource = attributes_to_dict(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            decoded = DecodedScope(
                resource=resource,
                scope_name=scope_spans.scope.name or "",
                scope_version=scope_spans.scope.version or "",
                spans=[_to_span(span) for span in scope_spans.spans],
            )
            scopes.append(decoded)
    return scopes


def _to_span(span: PbSpan) -> DecodedSpan:
    parent = span.parent_span_id.hex() if span.parent_span_id else None
    return DecodedSpan(
        trace_id=span.trace_id.hex(),
        span_id=span.span_id.hex(),
        # All-zero parent ids appear in the wild from exporters that set the field rather
        # than leaving it empty. Treated as absent, or the span becomes an orphan pointing
        # at a span that cannot exist.
        parent_span_id=parent if parent and set(parent) != {"0"} else None,
        name=span.name or "span",
        kind=PbSpan.SpanKind.Name(span.kind) if span.kind else "SPAN_KIND_UNSPECIFIED",
        start=_from_nanos(span.start_time_unix_nano),
        end=_from_nanos(span.end_time_unix_nano) if span.end_time_unix_nano else None,
        status_code=_STATUS_NAMES.get(int(span.status.code), "STATUS_CODE_UNSET"),
        status_message=span.status.message or None,
        attributes=attributes_to_dict(span.attributes),
        events=[
            DecodedEvent(
                name=event.name or "event",
                timestamp=_from_nanos(event.time_unix_nano),
                attributes=attributes_to_dict(event.attributes),
            )
            for event in span.events
        ],
        dropped_attributes=int(span.dropped_attributes_count),
        dropped_events=int(span.dropped_events_count),
    )


def _from_nanos(value: int) -> datetime:
    """Nanoseconds since the epoch to an aware datetime.

    Clamped rather than allowed to raise. A zero or absurd timestamp from a broken exporter
    should not take down a batch of otherwise-valid spans, and the epoch is a visibly wrong
    value that a reader will notice — unlike a silently dropped span.
    """
    try:
        return datetime.fromtimestamp(value / _NANOS, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return datetime.fromtimestamp(0, tz=UTC)


def attributes_to_dict(pairs: Any) -> dict[str, Any]:
    """Flatten OTLP `KeyValue` pairs into a plain dict."""
    out: dict[str, Any] = {}
    for pair in pairs:
        out[pair.key] = _any_value(pair.value)
    return out


def _any_value(value: AnyValue) -> Any:  # noqa: PLR0911 — one branch per union member
    """Unwrap OTLP's `AnyValue` union.

    `bytes_value` is base64-encoded rather than decoded to text. It is arbitrary binary by
    definition, and forcing it through a UTF-8 decode would either raise or produce
    mojibake — both worse than a string a reader can recognise as encoded.
    """
    which = value.WhichOneof("value")
    if which == "string_value":
        return value.string_value
    if which == "bool_value":
        return value.bool_value
    if which == "int_value":
        return value.int_value
    if which == "double_value":
        return value.double_value
    if which == "array_value":
        return [_any_value(item) for item in value.array_value.values]
    if which == "kvlist_value":
        return {item.key: _any_value(item.value) for item in value.kvlist_value.values}
    if which == "bytes_value":
        return base64.b64encode(value.bytes_value).decode("ascii")
    return None


def kv(key: str, value: Any) -> KeyValue:
    """Build a `KeyValue`, for tests and for the example collector config."""
    pair = KeyValue(key=key)
    if isinstance(value, bool):
        pair.value.bool_value = value
    elif isinstance(value, int):
        pair.value.int_value = value
    elif isinstance(value, float):
        pair.value.double_value = value
    else:
        pair.value.string_value = str(value)
    return pair


__all__ = [
    "JSON_CONTENT_TYPE",
    "PROTOBUF_CONTENT_TYPE",
    "DecodedEvent",
    "DecodedScope",
    "DecodedSpan",
    "OtlpDecodeError",
    "attributes_to_dict",
    "decode",
    "kv",
]
