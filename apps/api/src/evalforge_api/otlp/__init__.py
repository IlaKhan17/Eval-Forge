"""OTLP/HTTP receiver.

A translation layer over the native ingestion path, not a second one: `receiver.translate`
produces the same `IngestBatch` the native endpoint accepts, so OTLP traffic gets redaction,
payload offloading, and idempotent upserts from the same code and cannot drift from it.

- `decode` — OTLP protobuf or JSON to plain dataclasses
- `mapping` — OpenInference and OTel GenAI attributes to EvalForge span columns
- `receiver` — assembling a batch, including the traces OTLP has no concept of
"""

from evalforge_api.otlp.decode import DecodedScope, DecodedSpan, OtlpDecodeError, decode
from evalforge_api.otlp.mapping import MappedSpan, map_span, span_type_for, status_for
from evalforge_api.otlp.receiver import Translation, translate

__all__ = [
    "DecodedScope",
    "DecodedSpan",
    "MappedSpan",
    "OtlpDecodeError",
    "Translation",
    "decode",
    "map_span",
    "span_type_for",
    "status_for",
    "translate",
]
