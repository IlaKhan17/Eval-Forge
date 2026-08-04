# Sending traces with plain OpenTelemetry

If your application is already instrumented with OpenTelemetry, you do not need the EvalForge
SDK. Point the exporter at the receiver:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://your-evalforge/v1/otlp
OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer ef_prod_..."
```

That is the whole integration. `examples/langgraph-agent/agent.py` is a working agent using
nothing but `opentelemetry-sdk`, and it appears in the dashboard with correct span types,
token counts, cost, and a working span tree.

The endpoint stops at `/v1/otlp` because the exporter appends `/v1/traces` itself. An
endpoint that already ends in `/v1/traces` produces `/v1/traces/v1/traces` and a 404 on
every export.

## What the receiver is

A **translation layer**, not a second ingestion path. It builds the same `IngestBatch` the
native endpoint accepts and hands it to the same service, so OTLP traffic gets redaction,
payload offloading, idempotent upserts, and rollup recomputation from the same code — and
cannot drift from the native path, because there is nothing to drift.

Both encodings work. `application/x-protobuf` is what every SDK and the Collector default
to, and `application/json` is supported for hand-rolled clients and `curl`. A test asserts
the two produce identical rows: the same application must not report differently because of
a transport setting nobody thought was semantic.

## Two attribute conventions, both read

There is no single standard yet, and picking one would break half of what people run.

| | Used by | Example |
|---|---|---|
| **OpenInference** | Arize Phoenix, LlamaIndex, LangChain/LangGraph instrumentation | `openinference.span.kind`, `llm.model_name`, `llm.token_count.prompt` |
| **OTel GenAI semconv** | the official instrumentation libraries, where the standard is heading | `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` |

Where they disagree, **OpenInference wins**: a span carrying both is almost always
OpenInference instrumentation that also picked up a GenAI-shaped attribute from a library
underneath it, and the more specific producer is the one to trust.

### Span types

| Attribute value | EvalForge span type |
|---|---|
| `openinference.span.kind: LLM` / `gen_ai.operation.name: chat` | `llm` |
| `CHAIN` | `workflow` |
| `TOOL` / `execute_tool` | `tool` |
| `RETRIEVER`, `RERANKER` | `retriever` |
| `EMBEDDING` / `embeddings` | `embedding` |
| `AGENT` / `invoke_agent` | `agent` |
| `GUARDRAIL`, `EVALUATOR` | as named |
| anything else | `custom` |

A reranker maps to `retriever`, not `llm`. It has no tokens and no cost, and putting it in
the columns a cost dashboard sums would contribute zeroes that read as free model calls.

When no kind attribute is present, a few unambiguous signals are used: token counts imply
`llm`, a tool name implies `tool`, `retrieval.documents` implies `retriever`. Guessing
harder than that would mislabel spans, and a wrong `span_type` is worse than `custom` —
trajectory policies filter on it, so a mislabelled span silently drops out of a safety rule's
scope.

### Models, tokens, and cost

Read from either convention, including the older `gen_ai.usage.prompt_tokens` spelling that
released instrumentation still emits. The response model beats the requested one, because
what actually answered is what the cost should be attributed to.

A declared `llm.token_count.total` is kept when it is at least prompt + completion (cached
and reasoning tokens are real and are not in either), and recomputed when it is smaller.
Trusting it blindly lets a broken exporter report 0 for a span that clearly used tokens, and
cost dashboards are built on this number.

Set `llm.cost.total` if your application knows the price. Leaving it unset shows an empty
cost column, and nobody can tell whether that means free or unmeasured.

### Payloads

`input.value` and `output.value`, with `input.mime_type` / `output.mime_type`. **Set the MIME
type** for JSON payloads: it is what makes the receiver parse them into structure rather than
storing a JSON string, and evaluators resolve paths like `output.intent` — a string has no
paths inside it.

Without a MIME type, only text that unambiguously looks like JSON is parsed. A bare `"42"`
stays a string, because coercing it would silently change the type of data an evaluator
compares against.

On a **tool** span, `input.value` is also read as the tool's arguments, which is what
trajectory `args.*` predicates match on. On an LLM span it is not, so a prompt is never filed
as tool arguments.

### Nothing is discarded

Every attribute that is not promoted to a column stays in `attributes` verbatim, and a
promoted one is not stored twice under a second spelling. A receiver that silently drops the
attribute someone needs is worse than one that stores too much: the storage is cheap and the
debugging session is not.

The instrumentation scope is recorded as `otel.scope.name`, because "which instrumentation
produced this?" is the first question when a span maps oddly and OTLP puts the answer on the
scope rather than the span.

## OTLP has no concept of a trace

It carries spans; a trace is whatever set of spans share a trace id. So everything
trace-level is inferred:

- the **name** comes from the root span. A batch of child spans cannot name the trace, so it
  declares nothing and the trace is stubbed from its spans until the root arrives.
- **session and user** come from whichever span carries `session.id` / `user.id`, because
  OpenInference puts them on spans.
- **environment and commit** come from resource attributes — `deployment.environment.name`
  and `service.version`.
- the **extent** spans every span in the batch, not just the root's own times. A child that
  outlives its parent is normal, and clipping to the root would hide it.

`capture_mode` is always `redacted`. An OTLP client has no way to declare one, and assuming
`full` for traffic whose provenance we do not control would opt someone into storing raw
prompts without asking.

**Traces routinely arrive across several batches** — that is what `BatchSpanProcessor` does.
The receiver handles it, and a later batch of children can never overwrite the name, metadata,
or status an earlier declaration established. That was a real bug in the ingest service before
this phase: one `ON CONFLICT` clause served both declared and stubbed traces, so the second
batch of every multi-batch trace reset its name to "unknown".

`dropped_span_count` stays zero, because OTLP has no field for it — the SDK drops silently
and the Collector reports its drops in its own metrics. A trace that lost spans over OTLP
therefore looks complete, which matters when reading a trajectory verdict, and is why the
native SDK path reports it explicitly.

## Protocol behaviour

An OTLP client is a **retrying** client, so the response is not cosmetic.

| Situation | Response | Why |
|---|---|---|
| accepted | 200 + `ExportTraceServiceResponse` in the request's encoding | a bare `{}` makes some SDKs log a protocol error on every export |
| some spans rejected | 200 + `partial_success` | a partial failure is explicitly not an error status; 4xx would make the client retry the accepted spans too, forever |
| unparseable body | 400 | permanent; retrying can never succeed and clients honour that |
| storage failure | 503 | retryable, so a transient database problem does not cost the client its buffer |
| no `ingest` scope | 403 | |

Spans with malformed or all-zero ids are rejected individually rather than failing the
request, so one broken span does not discard the valid ones beside it. The rejection reason
goes in `partial_success.error_message` — the only channel a misconfigured exporter has for
finding out what is wrong.

Re-exporting the same batch does not double-count. Retries are the normal case, and a retry
that inflated the span count would make every metric derived from it wrong.

## Using a Collector

`infra/otel/collector-config.yaml` is a working pipeline. Use it if you already run a
Collector or want buffering and redaction inside your own network; otherwise point the
application straight at the receiver and skip the extra hop.

Two things in that file worth copying:

- **Scrub before sampling.** Scrubbing after would leave secrets in whatever the sampler kept.
  Redacting in the Collector is strictly better than relying on ingest-side redaction alone:
  data that never crosses the boundary cannot be stored by mistake.
- **Sampling stays at 100 %.** EvalForge samples on ingest for the *paid* evaluations and runs
  trajectory policies on every trace it receives, so head sampling here throws away coverage
  of the free safety checks. Turn it down only if trace volume itself is the problem.

## Trajectory policies work on OTLP traces

Nothing about the online-evaluation path knows where a trace came from. An OTLP-ingested
trace with a `gmail.send` and no `human.approve` fails an approval policy and lands in a
review queue exactly as an SDK trace does — verified end to end with the example agent.

## A trap worth knowing

Do not seed Python's global `random` in an instrumented process. OpenTelemetry's default id
generator draws from it, so seeding makes every run emit **identical trace and span ids** —
and the second run's spans merge into the first run's traces. It looks like the receiver is
losing data when it is deduplicating exactly as designed. The example carries a comment
about this because it happened while writing it.

## Not built yet

- **OTLP/gRPC.** HTTP only. gRPC needs a second server port and a second serialization path
  to test, and every SDK can speak HTTP.
- **Logs and metrics.** Traces only. EvalForge evaluates trajectories; a metrics pipeline is
  a different product.
- **`tracestate` and links.** Span links are dropped rather than stored; the schema has no
  place for them, and inventing one before something reads them would be speculative.
- **Server-side span limits per trace.** A single trace with a million spans is accepted a
  batch at a time. The batch limit bounds each request, not the total.
