"""Mapping OpenTelemetry attributes onto EvalForge span columns.

Pure functions: attributes in, span fields out. No protobuf, no database, no clock. The
wire decoding lives in `decode.py` and the assembly in `receiver.py`, so this file can be
tested against literal attribute dicts — which is how a mapping table should be tested,
because every bug in one is "this field silently ended up somewhere else".

## Two conventions, both mapped

There is no single standard here yet, and pretending otherwise would break half of what
people actually run:

- **OpenInference** (`openinference.span.kind`, `llm.model_name`, `llm.token_count.prompt`)
  is what Arize Phoenix, LlamaIndex, and most LangChain/LangGraph instrumentation emits
  today.
- **OTel GenAI semantic conventions** (`gen_ai.system`, `gen_ai.request.model`,
  `gen_ai.usage.input_tokens`) is where the standard is going, and what the official
  instrumentation libraries are moving to.

Both are read. Where they disagree, OpenInference wins, because a span carrying both is
almost always OpenInference instrumentation that also picked up a GenAI-shaped attribute
from a library underneath it — and the more specific producer is the one to trust.

## Nothing is thrown away

Every attribute that is not promoted to a column stays in `attributes` JSONB verbatim. A
receiver that silently drops the attribute someone needs is worse than one that stores too
much: the storage is cheap and the debugging session is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

# --------------------------------------------------------------------------- span kinds

#: OpenInference's span kinds, lowercased, mapped onto ours.
_OPENINFERENCE_KINDS: dict[str, str] = {
    "llm": "llm",
    "chain": "workflow",
    "tool": "tool",
    "retriever": "retriever",
    "embedding": "embedding",
    "agent": "agent",
    "guardrail": "guardrail",
    "evaluator": "evaluator",
    # A reranker is a retrieval step, not a model call. Mapping it to `llm` would put it in
    # the cost and token columns of a span that has neither.
    "reranker": "retriever",
    "unknown": "custom",
}

#: `gen_ai.operation.name` values, from the OTel GenAI conventions.
_GENAI_OPERATIONS: dict[str, str] = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "embeddings": "embedding",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "create_agent": "agent",
}

SPAN_KIND_KEYS = ("openinference.span.kind", "gen_ai.operation.name", "traceloop.span.kind")

# ------------------------------------------------------------------------------- models

MODEL_KEYS = (
    "llm.model_name",
    "gen_ai.response.model",
    "gen_ai.request.model",
    "llm.invocation_parameters.model",
    "embedding.model_name",
)
PROVIDER_KEYS = (
    "llm.provider",
    "gen_ai.system",
    "gen_ai.provider.name",
    "llm.system",
)

# `gen_ai.usage.input_tokens` is the current spelling; `prompt_tokens` was the earlier one
# and is still emitted by released instrumentation, so both are read.
PROMPT_TOKEN_KEYS = (
    "llm.token_count.prompt",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.prompt_tokens",
)
COMPLETION_TOKEN_KEYS = (
    "llm.token_count.completion",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
)
TOTAL_TOKEN_KEYS = ("llm.token_count.total",)

COST_KEYS = ("llm.cost.total", "gen_ai.usage.cost", "llm.cost")

# --------------------------------------------------------------------------- payloads

INPUT_KEYS = ("input.value", "gen_ai.prompt", "llm.input_messages", "traceloop.entity.input")
OUTPUT_KEYS = (
    "output.value",
    "gen_ai.completion",
    "llm.output_messages",
    "traceloop.entity.output",
)
INPUT_MIME_KEYS = ("input.mime_type",)
OUTPUT_MIME_KEYS = ("output.mime_type",)

TOOL_NAME_KEYS = ("tool.name", "gen_ai.tool.name", "tool_name")
TOOL_ARGS_KEYS = ("tool.parameters", "gen_ai.tool.arguments", "input.value")

SESSION_KEYS = ("session.id", "openinference.session.id", "gen_ai.conversation.id")
USER_KEYS = ("user.id", "openinference.user.id", "enduser.id")

#: Resource attributes that become trace-level fields.
SERVICE_NAME_KEY = "service.name"
ENVIRONMENT_KEYS = ("deployment.environment.name", "deployment.environment", "env")
GIT_COMMIT_KEYS = ("vcs.repository.ref.revision", "git.commit", "git.sha", "service.version")


@dataclass(slots=True)
class MappedSpan:
    """One OTLP span translated into the fields the ingest schema expects."""

    span_type: str = "custom"
    model: str | None = None
    provider: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: Decimal | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    input: Any = None
    output: Any = None
    session_id: str | None = None
    user_ref: str | None = None
    #: Everything not promoted to a column, kept verbatim.
    attributes: dict[str, Any] = field(default_factory=dict)


def first(attributes: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """The first present, non-empty value among `keys`.

    Order in the key tuples encodes precedence, which is why they are tuples rather than
    sets. Empty strings are skipped as absent: instrumentation that sets
    `llm.model_name: ""` means "I do not know", and letting that shadow a populated
    `gen_ai.request.model` would lose the model name.
    """
    for key in keys:
        value = attributes.get(key)
        if value is not None and value != "":
            return value
    return None


def span_type_for(  # noqa: PLR0911 — one return per documented signal, read top to bottom
    attributes: dict[str, Any], *, span_name: str = ""
) -> str:
    """Decide the EvalForge span type.

    Falls back to the span *name* only for a small set of unambiguous prefixes that
    OpenInference-less instrumentation uses. Guessing more aggressively than that would
    mislabel spans, and a wrong `span_type` is worse than `custom`: trajectory policies
    filter on it, so a mislabelled span can silently drop out of a safety rule's scope.
    """
    raw = first(attributes, SPAN_KIND_KEYS)
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key in _OPENINFERENCE_KINDS:
            return _OPENINFERENCE_KINDS[key]
        if key in _GENAI_OPERATIONS:
            return _GENAI_OPERATIONS[key]

    # A span with token counts is a model call whatever it calls itself.
    if any(k in attributes for k in PROMPT_TOKEN_KEYS + COMPLETION_TOKEN_KEYS):
        return "llm"
    if any(k in attributes for k in TOOL_NAME_KEYS):
        return "tool"
    if "embedding.embeddings" in attributes or "embedding.model_name" in attributes:
        return "embedding"
    if "retrieval.documents" in attributes:
        return "retriever"

    lowered = span_name.strip().lower()
    if lowered.startswith(("langgraph.", "graph.", "chain.")):
        return "workflow"
    return "custom"


def status_for(code: str, *, has_exception: bool = False) -> str:
    """Map an OTLP status code onto ours.

    `STATUS_CODE_UNSET` becomes `ok`, not `unset`, when there is no exception. That is a
    deliberate reading of the OTel spec: unset is the default for a span that completed
    without the instrumentation saying anything, and the overwhelming majority of such
    spans succeeded. Recording them all as `unset` would make the error rate meaningless by
    burying real errors in a sea of unknowns.

    An exception event overrides it, because a span that recorded an exception and left its
    status unset is an instrumentation gap, not a success.
    """
    if code == "STATUS_CODE_ERROR":
        return "error"
    if has_exception:
        return "error"
    if code == "STATUS_CODE_OK":
        return "ok"
    return "ok"


def map_span(attributes: dict[str, Any], *, span_name: str = "") -> MappedSpan:
    """Translate one span's attributes.

    The returned `attributes` dict holds everything that was *not* promoted, so the caller
    can store it losslessly without duplicating what is already in a column.
    """
    mapped = MappedSpan(span_type=span_type_for(attributes, span_name=span_name))
    consumed: set[str] = set()

    def take(keys: tuple[str, ...]) -> Any:
        value = first(attributes, keys)
        if value is not None:
            # Every key in the group is consumed, not just the winner. Leaving the losers in
            # `attributes` would store the same fact twice under two spellings, and a reader
            # comparing them would reasonably conclude they disagree.
            consumed.update(k for k in keys if k in attributes)
        return value

    mapped.model = _as_str(take(MODEL_KEYS), limit=200)
    mapped.provider = _as_str(take(PROVIDER_KEYS), limit=50)
    mapped.session_id = _as_str(take(SESSION_KEYS), limit=100)
    mapped.user_ref = _as_str(take(USER_KEYS), limit=200)

    mapped.prompt_tokens = _as_int(take(PROMPT_TOKEN_KEYS))
    mapped.completion_tokens = _as_int(take(COMPLETION_TOKEN_KEYS))
    declared_total = _as_int(take(TOTAL_TOKEN_KEYS))
    # Prefer the declared total when it is present and consistent; otherwise recompute.
    # Trusting a declared total blindly lets a broken exporter report 0 for a span that
    # clearly used tokens, and cost dashboards are built on this number.
    parts = mapped.prompt_tokens + mapped.completion_tokens
    mapped.total_tokens = declared_total if declared_total >= parts else parts

    mapped.cost = _as_decimal(take(COST_KEYS))

    mapped.tool_name = _as_str(take(TOOL_NAME_KEYS), limit=200)
    # `input.value` doubles as tool arguments for a tool span. Only consumed as arguments
    # when this really is a tool span, so an LLM span's prompt is not filed as tool args.
    args_keys = TOOL_ARGS_KEYS if mapped.span_type == "tool" else TOOL_ARGS_KEYS[:-1]
    mapped.tool_args = _as_object(take(args_keys))

    mapped.input = _decode_payload(take(INPUT_KEYS), mime=first(attributes, INPUT_MIME_KEYS))
    mapped.output = _decode_payload(take(OUTPUT_KEYS), mime=first(attributes, OUTPUT_MIME_KEYS))
    consumed.update(k for k in INPUT_MIME_KEYS + OUTPUT_MIME_KEYS if k in attributes)

    mapped.attributes = {k: v for k, v in attributes.items() if k not in consumed}
    return mapped


def trace_fields(resource: dict[str, Any]) -> dict[str, Any]:
    """Trace-level fields recoverable from resource attributes."""
    return {
        "service_name": _as_str(resource.get(SERVICE_NAME_KEY), limit=200),
        "environment": _as_str(first(resource, ENVIRONMENT_KEYS), limit=50),
        "git_commit": _as_str(first(resource, GIT_COMMIT_KEYS), limit=64),
    }


# ------------------------------------------------------------------------- coercion


def _as_str(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    # Truncated rather than rejected: a model name longer than the column is still worth
    # having, and dropping the span over it would lose everything else on it.
    return text[:limit] or None


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        # `int(float(...))` because some exporters send token counts as doubles — OTLP's
        # attribute type is a union and the choice is the exporter's.
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # A negative cost is meaningless and would corrupt a project's spend total.
    return cost if cost >= 0 else None


def _as_object(value: Any) -> dict[str, Any] | None:
    """Coerce to a JSON object, or None.

    Tool arguments are a mapping by definition. A JSON string is parsed; anything else that
    is not a mapping is wrapped rather than discarded, because "the exporter sent a list"
    should still be inspectable in the UI.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {"value": value}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {"value": value}


def _decode_payload(value: Any, *, mime: Any = None) -> Any:  # noqa: PLR0911
    """Parse a payload, using the declared MIME type as a hint.

    OpenInference sends `input.value` as a string plus `input.mime_type`. Parsing a JSON
    payload into structure matters for more than display: trajectory predicates and
    deterministic evaluators resolve paths like `output.intent`, and a JSON *string* has no
    paths inside it.

    A parse failure keeps the original string. Guessing that unparseable text was meant to
    be structured would be worse than showing what actually arrived.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value

    declared = str(mime).lower() if mime is not None else ""
    if "json" in declared:
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value

    # No MIME type: only parse when it unambiguously looks like JSON. Attempting a parse on
    # every string would turn the plain output "42" into an integer and "null" into None.
    trimmed = value.strip()
    if trimmed[:1] in ("{", "[") and trimmed[-1:] in ("}", "]"):
        try:
            return json.loads(trimmed)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


__all__ = [
    "MappedSpan",
    "first",
    "map_span",
    "span_type_for",
    "status_for",
    "trace_fields",
]
