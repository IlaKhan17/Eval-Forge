# EvalForge — Product Requirements

## 1. Problem

Teams shipping LLM applications and tool-using agents cannot answer, with evidence, whether a change made the system better or worse.

Concretely:

- **Non-determinism defeats normal tests.** A prompt tweak changes thousands of outputs in ways `assertEqual` cannot express.
- **Agents fail in the *middle*, not the end.** An agent can produce a perfect email and still have sent it before approval. Output-only evaluation is blind to this. This is the single largest gap in current tooling.
- **Logging dashboards describe, they don't decide.** Existing observability tools answer "what happened" and stop. Nobody is blocked from merging.
- **Evaluation is not connected to the change process.** Evals live in notebooks, run manually, and are skipped under deadline pressure.
- **Judges are trusted without evidence.** Teams adopt LLM-as-a-judge and never measure whether the judge agrees with a human. An uncalibrated judge is a random number generator with a confident tone.
- **Regressions hide inside averages.** Overall score 0.91 → 0.90 looks fine while unsubscribe-handling recall collapsed from 0.99 → 0.74 — a legal-exposure bug.

## 2. Target users

| Persona | Need | Primary surface |
|---|---|---|
| **AI application engineer** (primary) | Know if my prompt/model/retrieval change is safe to merge | CLI + PR comment |
| **Agent engineer** (primary, differentiated) | Prove my agent never takes side effects without approval | Trajectory policies |
| **Tech lead / reviewer** | Enforce quality bars without reading every diff | Quality gates, baselines |
| **Domain reviewer** (SDR manager, instructor) | Label outputs, correct them, build regression sets | Annotation queue |
| **Security engineer** | Test injection, leakage, tenant isolation, approval bypass | Security evaluators + adversarial datasets |
| **Self-hoster / OSS user** | Run the whole thing locally without a vendor account | `docker compose up`, local-only mode |

Not a target for the MVP: non-technical prompt authors, enterprise procurement, ML researchers doing model training.

## 3. Primary use cases

1. **Regression gate on a PR.** Change prompt → CI runs the suite against a locked dataset version → compares to the `main` baseline → fails the build when a protected metric regresses.
2. **Trajectory conformance.** Assert an agent's ordered tool calls satisfy a policy: approval precedes side effects, no forbidden tool, no runaway loops, no duplicate sends.
3. **Failure → dataset.** A production trace fails a check → reviewer inspects, labels, corrects → the example is promoted into a versioned golden dataset → the bug can never silently return.
4. **Candidate vs. baseline experiment.** Compare model A vs. model B, or prompt v7 vs. v8, on identical inputs with identical evaluators, with per-example diffs.
5. **Judge calibration.** Score a human-labelled set with an LLM judge; report agreement, false-pass and false-fail rates; refuse to gate on a judge that hasn't cleared a threshold.
6. **Production monitoring with bounded cost.** Every production trace gets deterministic policy/schema/security checks; a sampled slice gets expensive LLM judges.

## 4. Non-goals

**Never:** billing/metering, model hosting or fine-tuning, a general-purpose workflow engine, a prompt-authoring IDE, a no-code eval builder, an APM replacement for non-AI services, agent frameworks of our own.

**Deferred past MVP:** ClickHouse, Kubernetes manifests, multi-region, enterprise SSO/SCIM, TypeScript SDK, alerting/paging, >2 framework adapters, dataset labeling marketplace, RBAC beyond coarse roles, public shareable report links.

## 5. MVP definition

The MVP is exactly the 14-step loop in the brief, and nothing beyond it:

```
create project → issue API key → instrument with SDK → send nested-span trace
→ view trace in dashboard → create + lock dataset version
→ define deterministic + LLM evaluators → run experiment via CLI
→ upload results → compare candidate vs baseline → evaluate a trajectory
→ apply quality gates → non-zero exit on protected regression → same in GitHub Actions
```

### MVP inclusion criteria

A feature is in the MVP only if removing it breaks that loop. Applying this test to the brief:

| Brief item | MVP? | Why |
|---|---|---|
| Local eval engine, deterministic evaluators | **Yes** | Step 7–8 |
| LLM judge (rubric + classification) | **Yes** | Step 7 |
| Trajectory policy engine (YAML, order/forbid/limit) | **Yes** | Step 11, the differentiator |
| Quality gates + exit codes | **Yes** | Steps 12–13 |
| Python SDK tracing + async export | **Yes** | Steps 3–4 |
| REST ingestion, trace explorer | **Yes** | Steps 4–5 |
| Dataset versioning + locking | **Yes** | Step 6 |
| Experiment persistence + compare | **Yes** | Steps 9–10 |
| GitHub Actions + PR comment | **Yes** | Step 14 |
| API keys, org/project isolation, redaction | **Yes** | Safety floor, not a feature |
| **OTLP receiver** | **Phase 4b, behind the REST path** | Nice for adoption, not on the critical loop. Ship REST first, OTLP as an additive receiver that writes the same tables. |
| **Evaluator calibration** | **Phase 6, in MVP** | Without it, LLM gating is unsound. Minimum: agreement + false-pass/false-fail on a labelled set. |
| **Human annotation queues** | **Reduced.** Ship: annotate a trace/result, promote to dataset. Defer: assignments, inter-annotator agreement UI, pairwise preference UI. |
| **Online evaluation** | **Reduced.** Ship: deterministic + policy checks on ingested production traces, plus fixed-rate judge sampling. Defer: budgets, escalation, alerts. |
| Statistical evaluators (NDCG, MRR, calibration) | **Yes, as library functions** — they're pure code, cheap to write, and Davis's ranking suite needs them |
| Semantic (embedding) evaluators | **Defer to Phase 6+.** They occupy an awkward middle: less trustworthy than deterministic, less capable than judges, and add an embedding dependency. |
| Annotation pairwise preference, reviewer agreement | Defer |
| Alerting, webhooks | Defer (keep signed-webhook design so it isn't retrofitted insecurely) |

## 6. Later phases

- **v0.2** — OTLP receiver + Collector config; LangGraph adapter; embedding evaluators; richer annotation.
- **v0.3** — Online eval budgets and escalation; alerting; HTML reports; shareable links.
- **v0.4** — Analytics store migration path (ADR-006); partitioned trace tables; retention automation.
- **v1.0** — TypeScript SDK; SSO; multi-tenant hosted offering.

## 7. User stories with acceptance criteria

Format: *As a … I want … so that …* → **AC** (all must be objectively checkable).

### Epic A — Project & access

**A1.** As an engineer I want to create a project and issue an API key so my SDK can authenticate.
**AC:** Key is shown exactly once; only a hash is stored (ADR-003); key has a `ef_<env>_` prefix and an 8-char public identifier for lookup; revoking a key causes ingestion to 401 within one cache TTL (≤30 s); an audit-log row records issuance and revocation with actor and IP.

**A2.** As an org owner I want project-scoped isolation so one project's key cannot read another's traces.
**AC:** An automated cross-tenant test suite asserts 404 (not 403 — do not confirm existence) for every read endpoint when using a foreign key.

### Epic B — Tracing

**B1.** As an engineer I want to instrument a function with one decorator so I get a trace without restructuring code.
**AC:** `@evalforge.trace("name")` works on sync and async functions; nested calls produce correct parent/child spans; `asyncio.gather` children attach to the right parent (contextvar propagation test); exceptions record span status `error` and re-raise unchanged.

**B2.** As an engineer I want instrumentation that never breaks my app.
**AC:** With the API unreachable, the instrumented app's added latency is <5 ms p99 and it raises nothing; the exporter drops to a bounded in-memory buffer, logs once per backoff window (not per span), and resumes on recovery. A chaos test kills the API mid-run and asserts application success.

**B3.** As a security-conscious engineer I want capture modes so payloads never leave my process.
**AC:** `capture_mode` ∈ `full|redacted|metadata_only|disabled` at project and span level, most restrictive wins; a default deny-list redacts `authorization`, `api_key`, `token`, `password`, `cookie`, `secret`, `refresh_token` by key and by value-pattern; a unit test asserts a synthetic OAuth token never appears in an exported payload under any mode.

### Epic C — Datasets

**C1.** As an engineer I want immutable dataset versions so experiments are reproducible.
**AC:** After `lock`, any write to a version returns `409`; a `content_hash` over the canonically-serialized ordered examples is stored on lock; re-locking identical content yields an identical hash; an experiment records the version id **and** hash.

**C2.** As a reviewer I want to promote a failing production trace into a dataset.
**AC:** Promotion requires an explicit review action; the created example records `source_trace_id`; promotion into a locked version is rejected — it creates a draft version instead.

### Epic D — Evaluation & experiments

**D1.** As an engineer I want to run a suite locally with no server.
**AC:** `evalforge eval suite.yaml --local` runs with zero network calls to EvalForge, writes `evalforge-report.json`, prints a table, exits 0/1 correctly.

**D2.** As an engineer I want candidate-vs-baseline comparison with per-metric deltas.
**AC:** Report shows per-metric baseline, candidate, absolute and relative delta, and each gate's verdict; per-example regressions (passed in baseline, failed in candidate) are listed with span links.

**D3.** As a lead I want protected metrics that block regardless of the average.
**AC:** A suite where the mean improves but `unsubscribe_recall` drops below its minimum exits non-zero and names that metric as the blocking cause.

### Epic E — Trajectory policy

**E1.** As an agent engineer I want to assert approval precedes side effects.
**AC:** A trace with `gmail.send` before `approval_received` fails with a message naming the policy rule, the offending `span_id`, its timestamp, and the expected predecessor; a compliant trace passes; the same policy file yields identical verdicts locally and server-side (contract test).

**E2.** As an agent engineer I want loop and budget limits.
**AC:** `max_calls` violations report actual vs. allowed and the span ids of the excess calls; a repeated (tool, normalized-args) cycle ≥N times is reported as a loop.

### Epic F — Calibration

**F1.** As a lead I want to know whether a judge can be trusted before gating on it.
**AC:** Running a judge version against a human-labelled calibration set reports agreement, Cohen's κ, false-pass rate, false-fail rate, confusion matrix, cost and p95 latency; a gate on an uncalibrated judge emits a loud warning in CI (and can be configured to hard-fail).

### Epic G — CI

**G1.** As a maintainer I want a PR comment summarizing eval results.
**AC:** One comment per PR, updated in place (not appended); shows gate table, top regressions, and a link to the experiment; the job's exit code matches the gate verdict; the comment posts even when the run fails, reporting the failure.

## 8. Explicitly stated assumptions

1. MVP is single-region, single-writer Postgres; no HA requirement.
2. Reference integrations are examples, not products; Davis/AdaptQuiz code is out of scope beyond thin adapter examples.
3. "Baseline" defaults to the most recent successful experiment on the repo's default branch for the same suite name (see ADR-013).
4. Self-hosting is a first-class path; any feature requiring a hosted-only service is disqualified from the MVP.
5. Custom Python evaluators in the MVP run **in the user's own CLI process only** — never server-side (ADR-010). This is a hard boundary.
