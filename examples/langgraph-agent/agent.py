"""A LangGraph-shaped agent instrumented with plain OpenTelemetry — no Proofstep SDK.

This is the proof of the OTLP receiver's promise: an application that has never heard of
Proofstep, using only `opentelemetry-sdk` and OpenInference attribute conventions, appears
in the dashboard with correct span types, token counts, and a working span tree.

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8000/v1/otlp
    export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer ps_dev_..."
    uv run python examples/langgraph-agent/agent.py

Two deliberate choices in how this file is written:

**The graph is hand-rolled, not `langgraph` imported.** The receiver does not care which
framework produced the spans — it reads attributes — and an example that installs LangGraph,
LangChain, and a provider SDK to demonstrate an attribute mapping would obscure the thing it
is demonstrating. The span shape here is what
`openinference-instrumentation-langchain` emits, so swapping in the real library changes
nothing about what Proofstep sees.

**The model calls are simulated.** The example must run in CI with no provider key, and a
token count is a token count whether it came from a real response or a fixture. Swap
`_call_model` for a real client and nothing else changes.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Status, StatusCode

SERVICE_NAME = "langgraph-sdr-agent"


def configure_tracing() -> trace.Tracer:
    """Standard OpenTelemetry setup. Nothing Proofstep-specific.

    Falls back to the console exporter when no endpoint is configured, so running this file
    with no environment at all still shows what would have been sent — which is the whole
    point of an example.
    """
    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            # Becomes the trace's environment, and decides which online-eval rules apply.
            "deployment.environment.name": os.environ.get("ENVIRONMENT", "development"),
            # Becomes `git_commit`, which is what ties a trace to a deploy.
            "service.version": os.environ.get("GIT_COMMIT", "local"),
        }
    )
    provider = TracerProvider(resource=resource)

    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        # Imported here so the example still runs with only `opentelemetry-sdk` installed,
        # which is what the console-exporter fallback below is for.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        # Endpoint and headers come from the standard OTEL_* variables, which is the
        # one-line-of-configuration claim being demonstrated.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        print("OTEL_EXPORTER_OTLP_ENDPOINT is unset — printing spans instead of exporting\n")
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    return trace.get_tracer("openinference.instrumentation.langchain", "0.1.29")


@contextmanager
def span(tracer: trace.Tracer, name: str, kind: str, **attributes: Any) -> Iterator[trace.Span]:
    """One span with an OpenInference kind.

    `openinference.span.kind` is the single attribute that decides how Proofstep classifies
    the span. Everything else is refinement.
    """
    with tracer.start_as_current_span(name) as current:
        current.set_attribute("openinference.span.kind", kind)
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def _call_model(prompt: str, *, model: str) -> tuple[str, int, int]:  # noqa: ARG001
    """Simulated completion, returning the text and the token counts a real client reports.

    `model` is unused only because the response is fixed. It stays in the signature so
    swapping in a real client is a one-line change to the body.
    """
    time.sleep(0.05)
    if "unsubscribe" in prompt.lower() or "remove me" in prompt.lower():
        body = json.dumps({"intent": "unsubscribe", "reply": None})
    elif "pricing" in prompt.lower():
        body = json.dumps({"intent": "meeting_requested", "reply": "Thursday at 2pm?"})
    else:
        body = json.dumps({"intent": "not_interested", "reply": None})
    return body, len(prompt) // 4 + 120, len(body) // 4


def classify_intent(tracer: trace.Tracer, email: str) -> dict[str, Any]:
    """The model call. This is the span whose attributes matter most."""
    model = "claude-sonnet-5"
    with span(
        tracer,
        "ChatAnthropic",
        "LLM",
        **{
            "llm.model_name": model,
            "llm.provider": "anthropic",
            "llm.invocation_parameters": json.dumps({"temperature": 0, "max_tokens": 512}),
            "input.value": json.dumps({"messages": [{"role": "user", "content": email}]}),
            "input.mime_type": "application/json",
        },
    ) as current:
        body, prompt_tokens, completion_tokens = _call_model(email, model=model)

        # OpenInference's token attributes. These become the columns every cost and
        # efficiency number is computed from.
        current.set_attribute("llm.token_count.prompt", prompt_tokens)
        current.set_attribute("llm.token_count.completion", completion_tokens)
        current.set_attribute("llm.token_count.total", prompt_tokens + completion_tokens)
        # Optional, but if the application knows the price it should say so — otherwise the
        # cost column is empty and nobody can tell whether that means free or unmeasured.
        current.set_attribute(
            "llm.cost.total", f"{(prompt_tokens * 3 + completion_tokens * 15) / 1_000_000:.8f}"
        )

        # The MIME type is what tells Proofstep to parse this into structure rather than
        # storing a JSON string. Evaluators resolve paths like `output.intent`, and a string
        # has no paths inside it.
        current.set_attribute("output.value", body)
        current.set_attribute("output.mime_type", "application/json")
        return dict(json.loads(body))


def lookup_crm(tracer: trace.Tracer, email: str) -> dict[str, Any]:
    with span(
        tracer,
        "crm.lookup",
        "RETRIEVER",
        **{"input.value": json.dumps({"email": email})},
    ) as current:
        time.sleep(0.02)
        records = [{"company": "Northwind", "stage": "evaluating"}]
        current.set_attribute("retrieval.documents", json.dumps(records))
        current.set_attribute("output.value", json.dumps(records))
        current.set_attribute("output.mime_type", "application/json")
        return {"records": records}


def request_approval(tracer: trace.Tracer, draft: str) -> bool:
    with span(tracer, "human.approve", "TOOL", **{"tool.name": "human.approve"}) as current:
        time.sleep(0.01)
        current.set_attribute("output.value", json.dumps({"approved": True, "draft": draft[:80]}))
        current.set_attribute("output.mime_type", "application/json")
        return True


def send_email(tracer: trace.Tracer, to: str, body: str) -> None:
    with span(
        tracer,
        "gmail.send",
        "TOOL",
        **{
            "tool.name": "gmail.send",
            # For a tool span, `input.value` is read as the tool's arguments — which is what
            # trajectory policies match on with `args.*` predicates.
            "input.value": json.dumps({"to": to, "body": body}),
        },
    ) as current:
        time.sleep(0.01)
        if to.endswith("@suppressed.example"):
            # A recorded exception makes the span an error even if the status is never set,
            # and gives Proofstep the exception type rather than a bare "failed".
            error = RuntimeError("recipient is on the suppression list")
            current.record_exception(error)
            current.set_status(Status(StatusCode.ERROR, str(error)))
            raise error
        current.set_attribute("output.value", json.dumps({"message_id": "m-1"}))


def run(tracer: trace.Tracer, *, sender: str, email: str) -> str:
    """One turn of the agent, as one trace.

    The outermost span becomes the trace: its name is the trace name, and the session and
    user attributes on it become the trace's. OTLP has no trace concept, so this span is the
    only place that information can come from.
    """
    with span(
        tracer,
        "sdr.draft_reply",
        "AGENT",
        **{
            "session.id": f"conv-{abs(hash(sender)) % 10_000}",
            "user.id": sender,
            "input.value": json.dumps({"from": sender, "body": email}),
            "input.mime_type": "application/json",
        },
    ) as root:
        with span(tracer, "langgraph.invoke", "CHAIN"):
            lookup_crm(tracer, sender)
            classified = classify_intent(tracer, email)

        intent = str(classified.get("intent"))
        root.set_attribute("output.value", json.dumps(classified))
        root.set_attribute("output.mime_type", "application/json")

        # The interesting case for a trajectory policy: an unsubscribe must not be followed
        # by a send, and a send must be preceded by an approval.
        if intent == "unsubscribe":
            with span(tracer, "suppression.add", "TOOL", **{"tool.name": "suppression.add"}):
                time.sleep(0.01)
            return intent

        reply = str(classified.get("reply") or "")
        if reply:
            # `EXAMPLE_SKIP_APPROVAL=1` models the actual agent bug a trajectory policy
            # exists to catch: the send still happens, the approval step simply does not.
            # A flag that skipped the send as well would produce a *compliant* trace and
            # demonstrate nothing.
            if os.environ.get("EXAMPLE_SKIP_APPROVAL") != "1":
                request_approval(tracer, reply)
            send_email(tracer, sender, reply)
        return intent


def main() -> None:
    tracer = configure_tracing()

    # Deliberately *not* seeding `random`. OpenTelemetry's default id generator draws from
    # the global RNG, so seeding it makes every run emit identical trace and span ids — and
    # the second run's spans then merge into the first run's traces instead of creating new
    # ones. It looks like the receiver is losing data when in fact it is deduplicating
    # exactly as designed.
    inbox = [
        ("buyer@northwind.example", "Thanks — can you send pricing for 25 seats?"),
        ("cfo@acme.example", "Please remove me from your list."),
        ("ops@globex.example", "Not a fit for us right now."),
    ]
    for sender, email in inbox:
        intent = run(tracer, sender=sender, email=email)
        print(f"{sender:32} -> {intent}")

    provider = trace.get_tracer_provider()
    # Without this the process can exit before the batch processor flushes, and the spans
    # are simply lost — the most common way an OTLP example appears not to work.
    if hasattr(provider, "force_flush"):
        provider.force_flush()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    print("\nflushed")


if __name__ == "__main__":
    main()
