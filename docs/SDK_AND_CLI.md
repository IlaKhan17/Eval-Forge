# EvalForge — SDK, CLI, and Suite Configuration

## 1. Public Python API — tracing

Design goal: the smallest possible diff to an existing application. Three levels of intrusiveness, all producing the same spans.

```python
import evalforge

evalforge.init(                      # once, at startup; env vars are the default source
    api_key=os.environ["EVALFORGE_API_KEY"],
    project="davis",
    environment="production",
    capture_mode="redacted",         # full | redacted | metadata_only | disabled
    sample_rate=1.0,
    enabled=True,                    # EVALFORGE_ENABLED=0 kills it entirely
)

# Level 1 — decorator
@evalforge.trace("generate_outreach")
async def generate_outreach(prospect_id: str) -> Email: ...

@evalforge.span("research_prospect", span_type="agent")
async def research(prospect_id: str) -> Research: ...

@evalforge.tool("gmail.send")        # records tool_name + args; enables policies
async def send_email(to: str, subject: str, body: str) -> str: ...

# Level 2 — context managers
with evalforge.trace("generate_outreach") as t:
    t.set_metadata(prospect_id=pid, icp_version="v4")
    with t.span("research_prospect", span_type="agent") as s:
        s.set_input({"prospect_id": pid})
        result = await research(pid)
        s.set_output(result)
    t.set_state(approval_status="approved")     # feeds final_state policy rules

# Level 3 — manual
span = evalforge.start_span("custom", span_type="custom", parent=other)
span.end(status="ok")
```

Auxiliary surface:

```python
evalforge.current_trace() -> Trace | None
evalforge.current_span()  -> Span | None
evalforge.record_event("retry", attempt=2)          # → span_events
evalforge.set_state(approval_status="approved")
evalforge.flush(timeout=5.0)                        # before process exit
evalforge.shutdown()
@evalforge.redact("body", "recipient")              # field-level opt-out
```

Design decisions worth stating:

- **`@evalforge.tool` is separate from `@evalforge.span`.** It sets `span_type="tool"` and `tool_name`, and captures the call arguments as `args`. This is what the policy engine consumes, so making it a distinct, obvious decorator is what makes trajectory policies usable rather than a configuration exercise.
- **`set_state`** exists so `final_state` and `conditional` policy rules have a defined data source instead of scraping outputs.
- **Never raises.** Every public entry point is wrapped: an internal error logs once (rate-limited) and returns a no-op span. A telemetry library that can crash the host application is unusable, and this is non-negotiable.
- **Sync and async.** The decorator inspects the wrapped function and returns the matching wrapper; context managers implement both `__enter__` and `__aenter__`.

## 2. Context propagation

`contextvars.ContextVar[SpanContext]`. `asyncio.create_task` and `TaskGroup` copy the context automatically, so children attach correctly with no user action — this is the main reason for contextvars over thread-locals.

- **Threads:** `evalforge.propagate(fn)` wraps a callable to carry the current context; `ThreadPoolExecutor` users call it explicitly (Python does not copy contextvars across threads).
- **Cross-service:** W3C `traceparent` header. `evalforge.inject(headers)` / `evalforge.extract(headers)`, plus an optional httpx/requests hook that injects automatically.
- **Celery/ARQ:** helpers to serialize the context into the job payload.

## 3. Exporter

```
span.end() → redact → serialize → size check → bounded ring buffer (10k)
           → background asyncio task (or thread for sync apps)
           → batch on 512 spans or 2 s, gzip → POST /v1/ingest/traces
           → on 5xx/timeout: exponential backoff + full jitter, 5 attempts
           → on persistent failure: optional disk spool (~/.evalforge/spool), else drop-oldest
```

- **Never blocks the caller.** `span.end()` is a non-blocking enqueue; if the buffer is full it drops and increments a counter (reported in the trace's `dropped_span_count` and logged once per minute). Visible loss beats invisible stalls.
- **Offline behaviour.** With the API unreachable, the app runs normally. Optional disk spool (off by default, on in `--local` CI mode) replays on the next successful connection. Backoff logging is once per window, not per span — a common and infuriating failure of telemetry libraries.
- **Payload caps.** Per-field 256 KiB, per-span 1 MiB, per-batch 5 MiB. Oversize fields are truncated with `{"_truncated": true, "_original_size": N, "_sha256": "…"}` so the loss is recorded rather than silent.
- **`atexit` + signal handlers** flush with a 5 s timeout.
- **Sampling:** head-based on `trace_id`, deterministic (hash-mod), so a sampled trace is complete rather than partially captured. Errors and policy-relevant traces are always kept (`always_sample_on_error=True`).

## 4. Redaction

Runs **in the SDK, before export** — the strongest privacy guarantee available, since redacted data never leaves the process. A second server-side pass is defence in depth, not the primary control.

Pipeline: key deny-list (case-insensitive substring on `authorization`, `api_key`, `apikey`, `token`, `secret`, `password`, `passwd`, `cookie`, `session`, `refresh_token`, `client_secret`, `private_key`, `ssn`, `credit_card`) → value patterns (JWT, `sk-*`/`ghp_*`-style provider keys, PEM blocks, AWS keys, high-entropy strings ≥32 chars matching base64/hex charsets) → optional PII patterns (email, phone, IBAN, card) → user hooks.

```python
evalforge.init(redactors=[
    evalforge.redactors.default(),
    evalforge.redactors.keys(["prospect_email"]),
    evalforge.redactors.regex(r"CUST-\d{8}", replacement="[CUSTOMER_ID]"),
    my_custom_redactor,                 # (path: str, value: Any) -> Any | REDACTED
])
```

Replacement is `"[REDACTED:<reason>]"`, and a `redaction_count` per span makes the redaction itself visible. Capture modes: `full` (no redaction beyond secrets — secrets are *always* redacted, in every mode), `redacted` (default), `metadata_only` (names, timings, tokens, cost, tool names, no payloads), `disabled`.

## 5. Local evaluation API

Two entry points. The imperative one for scripts, the decorator one for suites.

```python
from evalforge import evaluate, Dataset
from evalforge.evaluators import exact_match, json_schema, llm_judge

result = await evaluate(
    dataset=Dataset.from_jsonl("reply-intent.jsonl"),
    task=classify_reply,
    evaluators=[exact_match(field="intent"), unsubscribe_recall],
    concurrency=8,
    timeout_s=60,
)
print(result.summary()); result.to_json("report.json")
```

```python
from evalforge import EvalSuite, Dataset

suite = EvalSuite(name="reply-intent", dataset=Dataset.from_jsonl("reply-intent.jsonl"))

@suite.task
async def classify(example):
    return await reply_agent.classify(example.input["email_body"])

@suite.evaluator(name="intent_accuracy")
def accuracy(output, expected):
    return float(output["intent"] == expected["intent"])

@suite.evaluator(name="unsubscribe_recall", aggregation="recall",
                 slice_when=lambda ex: ex.expected["intent"] == "unsubscribe")
def unsub(output, expected):
    return float(output["intent"] == expected["intent"])

@suite.gate("unsubscribe_recall", minimum=0.98, blocking=True)
@suite.gate("intent_accuracy", minimum=0.85, max_regression=0.02)
class _: pass

result = await suite.run()
```

Evaluator function signatures are resolved by parameter name, so users write only what they need: `(output)`, `(output, expected)`, `(output, expected, example)`, or `(ctx)` for the full context including `trace`. Introspection-based signatures keep the common case to one line while leaving the full context reachable — the alternative (always passing a context object) makes trivial evaluators verbose.

Returns are coerced: `bool` → 0.0/1.0, `float` → score, `Score` → verbatim, `dict` → `Score(**d)`.

`EvalResult` exposes `.metrics`, `.examples`, `.gates`, `.verdict`, `.exit_code`, `.summary()`, `.to_json()`, `.compare(baseline)`.

## 6. Suite YAML

```yaml
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: sdr-email-quality
description: Gates outbound email generation quality.

extends: suites/_base.yaml            # composition: shallow-merge, lists replace

dataset:
  name: email-quality
  version: v3                          # locked version; "latest-locked" also allowed
  # or: path: fixtures/email-quality.jsonl   (local-only)
  split: test

task:
  entrypoint: davis.evals:generate_email
  timeout_s: 120
  retries: 2

configuration:
  prompt_version: email-v8
  model: ${EMAIL_MODEL:-<default-model-id>}
  temperature: 0.2

execution:
  concurrency: 8
  judge_concurrency: 4
  max_error_rate: 0.10
  seed: 42

evaluators:
  - name: valid_schema
    type: json_schema
    schema: schemas/email.json

  - name: no_placeholders
    type: regex
    field: output.body
    deny: ["\\[Your Name\\]", "\\[Company\\]", "\\{\\{.*?\\}\\}"]

  - name: body_length
    type: length
    field: output.body
    unit: words
    min: 40
    max: 160

  - name: grounded_personalization
    type: llm_judge
    mode: rubric
    rubric: rubrics/groundedness.md
    model: ${JUDGE_MODEL}
    temperature: 0
    inputs: [output.body, input.evidence]
    scale: {min: 1, max: 5, normalize: true}
    calibration:
      dataset: email-groundedness-calibration
      version: v2
      require: {min_agreement: 0.80, max_false_pass_rate: 0.05}

  - name: approval_trajectory
    type: trajectory
    policy: policies/email-approval.yaml

  - name: cost_per_email
    type: operational
    metric: cost

gates:
  valid_schema:        {minimum: 1.0, blocking: true}
  no_placeholders:     {minimum: 1.0, blocking: true}
  approval_trajectory: {minimum: 1.0, blocking: true}
  grounded_personalization: {minimum: 0.90, max_regression: 0.02}
  cost_per_email:      {maximum: 0.02, blocking: false}
  p95_latency_ms:      {maximum: 5000}

baseline:
  strategy: latest_on_branch
  branch: main
  require_dataset_match: true

report:
  formats: [terminal, json]
  output: evalforge-report.json
```

**Validation:** JSON Schema + semantic checks at load, before any model call. Errors carry file, line, and column. Semantic checks that catch real mistakes: a gate whose `metric_key` matches no evaluator (**error**, not warning); an evaluator referencing a missing schema/rubric/policy file; a `max_regression` gate with no baseline strategy; a judge without `inputs`; a locked-version reference that doesn't exist. Failing fast here saves an entire expensive run.

**Env interpolation:** `${VAR}` and `${VAR:-default}`, resolved at load. A missing `${VAR}` with no default is an error. **Secrets are never interpolated into stored config** — the suite is uploaded with `${...}` intact, and only resolved values that are not flagged secret are persisted. Provider API keys come from the environment and are never written to a report, a log, or the server.

**Overrides:** `--set execution.concurrency=16 --set configuration.model=X`, dotted paths, typed by the schema.
**Composition:** `extends` for a base file; `include:` for shared evaluator lists. One level of inheritance only — deep hierarchies in config files are a well-known trap.

## 7. CLI

```bash
evalforge configure                 # interactive; writes ~/.evalforge/config.toml
evalforge login                     # device-code flow; token in the OS keyring
evalforge whoami
evalforge projects list|create
evalforge datasets list|show|push|pull|lock|import|export
evalforge evaluators list|push|calibrate
evalforge policies validate|test
evalforge eval <suite.yaml>
evalforge experiments list|show|compare A B|promote-baseline <id>
evalforge traces list|show|export
evalforge doctor                    # env + connectivity + config diagnosis
```

Core command:

```bash
evalforge eval evals/suites/sdr-email.yaml \
  --baseline main \
  --candidate HEAD \
  --output evalforge-report.json \
  --concurrency 8 \
  [--local] [--dry-run] [--resume RUN_ID] [--only-failed RUN_ID] \
  [--filter 'metadata.segment==enterprise'] [--limit 20] [--set k=v]
```

Sequence: load+validate config → resolve dataset version → resolve baseline → register evaluator/policy versions → open experiment+run → execute (journaled) → aggregate → compare → gates → report → exit.

### Publishing

**Implemented.** `eval` records the run on the server whenever `EVALFORGE_ENDPOINT` and `EVALFORGE_API_KEY` are both set. `--local` opts out; there is no flag to opt *in*, because a run that needs a flag to be recorded is a run nobody records.

What it does, in order — before the run: resolve the baseline for this suite on the baseline branch and load its metrics, so regression gates fire in the same process that produces the exit code. After the run: ensure the dataset and a **content-addressed** version (`sha-<12 hex>` of the examples, so identical data reuses a version and changed data cannot reuse a label), mirror the suite's gates, open an experiment and run, upload results in batches, complete the run, submit the corpus metrics the server cannot derive, and compare.

Four rules it is built on, each of which is a decision rather than an implementation detail:

1. **Publishing never changes the verdict.** The exit code is the local evaluation's. A slow, unreachable, or misconfigured server must not be able to turn a failing run into a passing one, or the gate would depend on infrastructure rather than on the code being merged. `--require-publish` can make a *passing* run fail when the record could not be written; nothing can make a failing run pass.
2. **A failed publish is reported, never swallowed** — on stderr, with the endpoint and the reason. The failure mode being designed against is someone believing a record exists.
3. **Dataset versions are content-addressed**, which is what makes `dataset_match` mean something. If the server's hash of the same examples ever disagrees with the local one, publishing stops rather than recording a comparison across different data.
4. **The server's verdict is checked against the local one** and any difference is printed loudly. They are computed by the same code from the same numbers, so a disagreement is a bug in this system — the one bug that discredits every other number it reports. The exception is a server that resolved a baseline this run did not have; that is reported as context rather than as a disagreement.

Two server capabilities exist for this and are worth knowing about directly:

- `POST /v1/experiment-runs/{id}/metrics` accepts metrics the server **cannot** compute — corpus and operational ones like a confusion matrix, per-class recall, or p95 latency, which are properties of the whole run rather than sums over per-example scores. Anything derived from scores is recomputed server-side and a submission for it is refused, so a client cannot overwrite a number the server verified. Without this, a suite gating on a protected class's recall publishes and then reads as ERROR because the metric is missing.
- `GET /v1/experiments/baseline?suite_name=&branch=` resolves the run a candidate will be compared against, with its metrics, and answers `run_id: null` rather than 404 when there is none — the first run of a suite is the normal case, not an error.

`--dry-run` validates everything, resolves the baseline, and prints the plan with an estimated cost — without a single model call. Given that a full suite can cost real money, being able to check the wiring for free is a requirement, not a nicety.

Terminal report:

```
EvalForge · sdr-email-quality
dataset email-quality@v3 (200 examples, sha 4f2a…)   commit a1b2c3d (dirty)
baseline exp_018e… (main, 2 days ago)

METRIC                        BASELINE  CANDIDATE     DELTA  GATE
valid_schema                     1.000      1.000     +0.000  ✓ pass   min 1.00
no_placeholders                  1.000      0.995     -0.005  ✗ FAIL   min 1.00  [blocking]
grounded_personalization         0.930      0.911     -0.019  ✓ pass   min 0.90, maxΔ 0.02
approval_trajectory              1.000      0.990     -0.010  ✗ FAIL   min 1.00  [blocking]
cost_per_email                  $0.0131    $0.0158    +20.6%  ⚠ warn   max $0.02
p95_latency_ms                    3210       4890    +52.3%   ✓ pass   max 5000

⚠ grounded_personalization: judge calibration is 41 days old (agreement 0.84)

2 blocking failures · 4 regressed examples

✗ approval_trajectory  ex-118  gmail.send before approval_received (span 7f3a2b1c)
✗ no_placeholders      ex-042  matched "[Company]" in output.body

Report: evalforge-report.json   Experiment: https://…/exp_018f…
exit 1
```

Design: the gate column carries the threshold, so the reader never has to open the YAML to interpret a failure. Regressed examples are listed inline with the concrete reason. Colour is disabled under `CI=true`/`NO_COLOR`, and unicode degrades to ASCII when the terminal encoding can't handle it.

`evalforge-report.json` is versioned (`report_version: 1`) and is the contract consumed by the GitHub Action, so it gets its own JSON Schema and contract tests.

## 8. GitHub Actions

```yaml
name: EvalForge
on: pull_request
permissions: {contents: read, pull-requests: write}
jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: {fetch-depth: 0}
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen
      - uses: evalforge/evalforge-action@v1
        with:
          suite: evals/suites/sdr-email.yaml
          baseline: main
          api-key: ${{ secrets.EVALFORGE_API_KEY }}
          comment: true
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

The action is a thin composite wrapper: install CLI → `evalforge eval` → parse the report → upload as an artifact → upsert one PR comment (found by an HTML marker comment, edited in place, never appended) → set the job status from the exit code. If the run itself errors, the comment reports the error rather than staying silent — a missing comment reads as "no problems", which is the wrong default.

**Fork PRs have no secrets.** Documented pattern: run the eval on `pull_request_target` with an explicit maintainer `approved-for-eval` label gate, or skip evaluation on forks and gate on merge to a staging branch. Do not paper over this — running untrusted PR code with production secrets is a supply-chain compromise, and the docs will say so directly.

No GitHub App in the MVP: a PAT/`GITHUB_TOKEN` plus the Actions workflow covers every MVP requirement, and an App adds installation flows, webhook endpoints, and secret custody for zero incremental capability.
