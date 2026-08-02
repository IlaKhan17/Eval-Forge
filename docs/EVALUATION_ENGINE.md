# EvalForge — Evaluation Engine Design

`packages/evaluation-core` — a **pure library**. No HTTP, no database, no provider SDKs. Model access arrives through an injected protocol. This is the single most important boundary in the system: it is what makes local mode, CI mode, and server mode the same code, and what makes the whole engine unit-testable without a network.

## 1. Core abstractions

```python
# shared_types / evaluation_core.types

class Example(BaseModel):
    id: str                      # stable external id
    input: dict[str, Any]
    expected: dict[str, Any] | None = None
    metadata: dict[str, Any] = {}

class TaskOutput(BaseModel):
    output: Any
    trace: TrajectoryTrace | None = None    # captured spans, if instrumented
    latency_ms: int
    tokens: TokenUsage | None = None
    cost: Decimal | None = None
    error: TaskError | None = None

class EvalContext(BaseModel):
    example: Example
    output: Any
    expected: dict[str, Any] | None
    trace: TrajectoryTrace | None
    metadata: dict[str, Any]
    # injected services
    models: ModelClient          # protocol, not a concrete SDK
    logger: Logger

class Score(BaseModel):
    value: float | None          # normalized [0,1] where meaningful
    passed: bool | None
    label: str | None = None     # categorical evaluators
    raw: Any = None              # non-scalar results (confusion matrix, list)
    reasoning: str | None = None
    confidence: float | None = None
    cost: Decimal = Decimal(0)
    latency_ms: int = 0
    error: str | None = None     # evaluator itself failed — distinct from score 0
```

**`error is not None` must never be conflated with `value = 0.0`.** A judge that timed out is not a failing example. Aggregation excludes errored evaluations from the mean and reports `error_count` separately; a gate on a metric with >5 % evaluator errors fails as `error`, not `pass`. Silently scoring infrastructure failures as zero is the fastest way to make a gate untrustworthy.

### Protocols

```python
class Task(Protocol):
    async def __call__(self, example: Example) -> Any: ...

class Evaluator(Protocol):
    name: str
    version: int
    aggregation: Aggregation          # how per-example scores roll up
    requires_trace: bool = False
    requires_expected: bool = False
    async def evaluate(self, ctx: EvalContext) -> Score | list[Score]: ...

class Aggregator(Protocol):
    def aggregate(self, scores: Sequence[Score]) -> list[Metric]: ...

class ModelClient(Protocol):
    async def complete(self, *, model: str, messages: list[Message],
                       response_format: type[BaseModel] | None = None,
                       temperature: float = 0.0, seed: int | None = None,
                       timeout: float = 60.0) -> ModelResponse: ...
```

Returning `list[Score]` lets one evaluator emit multiple metrics (a classification evaluator emits macro-F1 plus per-class recall) without inventing a second abstraction.

**Corpus-level evaluators.** Some metrics (F1, NDCG, calibration) are not per-example means. These implement a second protocol:

```python
class CorpusEvaluator(Protocol):
    name: str; version: int
    def evaluate_corpus(self, results: Sequence[ExampleResult]) -> list[Metric]: ...
```

Treating F1 as "mean of per-example F1" is a real and common statistical error — a corpus metric needs the full confusion matrix, so the engine must model it as a distinct stage rather than forcing it through per-example aggregation.

## 2. Evaluator catalogue

### 2.1 Deterministic (run 100 % of the time, free, always first)

| Name | Config | Output |
|---|---|---|
| `exact_match` | `field`, `normalize` (case/whitespace/punct) | binary |
| `json_schema` | `schema` (path or inline) | binary + error list |
| `regex` | `allow[]`, `deny[]`, `flags`, `field` | binary + matched pattern |
| `contains` | `substrings[]`, `mode: all\|any`, `case_sensitive` | binary |
| `length` | `field`, `min`, `max`, `unit: chars\|words\|tokens` | binary + actual |
| `numeric_range` | `field`, `min`, `max`, `inclusive` | binary |
| `set_comparison` | `field`, `mode: equals\|subset\|superset\|jaccard` | binary or [0,1] |
| `custom_python` | `entrypoint: module:function` | any |
| `business_rule` | declarative predicate tree over output+metadata | binary |

`business_rule` exists so that "if `reply_intent == unsubscribe` then `followup_generated == false`" doesn't require writing Python. It is a small predicate expression evaluated over a restricted AST — the same substrate as the policy engine's `conditions`, reused rather than reimplemented.

**Where ordinary tests belong instead.** These evaluators are for probabilistic outputs. If a function deterministically produces JSON and you want to assert the schema, that is a `pytest` unit test, not an eval. Running a dataset of 200 examples through a judge to discover that your serializer emits `null` is expensive and slow. Guidance shipped in the docs: *if the assertion would hold for every input given correct code, write a unit test; if it holds only statistically, write an eval.*

### 2.2 Statistical / corpus

`accuracy`, `precision`, `recall`, `f1` (macro/micro/weighted/per-class), `confusion_matrix`, `precision_at_k`, `recall_at_k`, `ndcg_at_k`, `mrr`, `map`, `expected_calibration_error`, `brier_score`.

Implemented as pure numpy-free Python where feasible to keep the core dependency-light (only ranking metrics justify a numeric dep; scipy is not needed). Confidence intervals: bootstrap (10 000 resamples, seeded) for means and Wilson intervals for proportions — both cheap and correct for the sample sizes involved (n = 50–5 000).

### 2.3 Semantic (deferred to Phase 6+)

`embedding_similarity`, `answer_relevance`, `context_relevance`, `retrieval_relevance`. Requires an `EmbeddingClient` protocol. Deferred because they are less interpretable than deterministic checks and less capable than judges, while adding a dependency and a cache-management problem.

### 2.4 LLM-as-a-judge

```yaml
- name: grounded_personalization
  type: llm_judge
  mode: rubric            # rubric | classify | pairwise | binary
  model: <pinned model id>
  temperature: 0
  seed: 42
  rubric: rubrics/groundedness.md
  scale: {min: 1, max: 5, normalize: true}
  inputs: [output.email_body, input.evidence]     # explicit — nothing else is sent
  votes: 1                                        # >1 → self-consistency, median
  max_retries: 2
  timeout_s: 60
```

Design requirements, each addressing a specific failure mode:

1. **Structured output.** The judge is forced into a JSON schema (`{score, reasoning, evidence_spans}`) via tool/structured-output mode. Never parse free text — parse failures become silent zeros.
2. **Reasoning before score.** The schema orders `reasoning` first so the model reasons before committing, then the score. Field order in the schema materially changes quality.
3. **Explicit input allow-list.** `inputs` enumerates exactly which fields reach the judge. A judge that receives the whole example can read `expected` and grade itself — an easy, catastrophic leak.
4. **Injection defence.** Evaluated content is wrapped in delimited blocks with an instruction that content inside is data, never instructions. The judge's structured-output schema means an injected "output SCORE: 5" cannot escape into the score field. Additionally a `judge_injection_canary` check: if the model's reasoning references the delimiter or meta-instructions, flag it. This is mitigation, not a solution — see `SECURITY.md` §6.
5. **Determinism.** `temperature=0`, pinned model *version string*, `seed` where supported. The model id in the evaluator version is the reproducibility anchor.
6. **Cost/latency accounting.** Every judge call records tokens, cost, and latency; suites report total judge spend. A judge that costs more than the task it evaluates should be a visible fact.
7. **Position-bias control** for pairwise: each pair is evaluated in both orders and disagreement is reported as a tie. Pairwise judges without order-swapping are measurably biased.

Judge calls are themselves traced (`span_type=evaluator`), so the exact prompt is inspectable.

### 2.5 Trajectory

Delegates entirely to `packages/trajectory-engine`. The evaluator is a thin adapter: load policy version → normalize `ctx.trace` → evaluate → map failures to `Score(value=0/1, raw=failures)`. See `TRAJECTORY_POLICIES.md`.

### 2.6 Security

`prompt_injection_detected`, `secret_leakage`, `pii_leakage`, `cross_tenant_reference`, `approval_bypass`, `duplicate_side_effect`, `suppression_violation`, `unauthorized_tool`, `dangerous_tool_argument`.

Mostly deterministic: pattern/entropy detection for secrets, regex + optional NER for PII, and **trajectory rules** for the agent-security ones (approval bypass, duplicate side effect, unauthorized tool are all trajectory predicates — implementing them anywhere else would duplicate the engine). Security evaluators default to `blocking: true` and `minimum: 1.0` / `maximum: 0`.

### 2.7 Operational

`latency_p50/p95/p99`, `total_tokens`, `cost_per_example`, `error_rate`, `retry_count`, `tool_call_count`, `model_call_count`, `cache_hit_rate`. All computed from the captured trace with zero model calls. Corpus-level (percentiles are not means).

## 3. Naming, versioning, configuration, storage

- **Name:** `slug` unique per project; the metric key in gates is the evaluator name (or `name.submetric` for multi-output, e.g. `intent_f1.recall.unsubscribe`).
- **Version:** monotonic integer per evaluator; bumped automatically when `config_hash` changes. The config hash covers *everything* affecting a score, including judge model and temperature.
- **Configure:** YAML in the suite (source of truth in the repo) → registered/upserted to the server on run, so the server copy is a mirror of git rather than a second place to edit. Config drift between repo and dashboard is a category of bug we simply refuse to have.
- **Execute:** async, concurrency-capped, per-evaluator timeout, retries only for transport errors (never for a low score).
- **Trace:** every evaluation emits an `evaluator` span.
- **Calibrate:** §5.
- **Store:** `evaluation_results` (per example) + `aggregate_metrics` (rollups).
- **Compare:** by `(metric_key, slice)` across two runs.

## 4. Execution model

```
load suite → resolve dataset version (locked; else refuse with --allow-draft)
  → build evaluator instances from versioned configs
  → open experiment + run
  → asyncio.TaskGroup over examples, bounded by Semaphore(concurrency)
       per example:
         with tracing_context():
             output = await run_task(example)      # timeout, retries
         deterministic evaluators  (parallel, cheap)
         trajectory evaluators     (local, cheap)
         judge evaluators          (parallel, separate semaphore + rate limiter)
       → ExampleResult  (streamed to an on-disk JSONL journal immediately)
  → corpus evaluators over all ExampleResults
  → aggregate → metrics
  → gates (candidate vs baseline)
  → report (terminal + JSON) → exit code
```

Details that matter:

- **Two semaphores.** Task concurrency and judge concurrency are separate limits. They contend for different resources (the user's app vs. the judge provider's rate limit) and coupling them means one throttles the other.
- **Journaling.** Each `ExampleResult` is appended to `.evalforge/runs/<run_id>.jsonl` as it completes. A crash at example 190/200 loses nothing; `evalforge eval --resume <run_id>` skips completed ids. This costs ~20 lines and eliminates the worst developer experience in eval tooling (losing a 40-minute, $12 run to a transient 429).
- **Retries.** Task retries on `TimeoutError`/connection errors with exponential backoff + full jitter, default 2 attempts, configurable. **Never** retry on a low score. `retry_count` is recorded and is itself an operational metric.
- **Timeouts.** Per-example (default 120 s) and per-evaluator (default 60 s). A hung example is marked `timeout`, not silently dropped.
- **Cancellation.** `SIGINT` cancels the TaskGroup, drains in-flight work with a 10 s grace period, finalizes the run as `cancelled`, writes a partial report, and exits 130. A second `SIGINT` exits immediately.
- **Partial failure policy.** `on_task_error: fail_example | fail_run` (default `fail_example`). If more than `max_error_rate` (default 0.10) of examples error, the run is `failed`, not `partial` — a run where a third of examples crashed must not report a cheerful average over the survivors.
- **Determinism.** Seed propagated to task and judges; `PYTHONHASHSEED` set; examples processed in `ordinal` order for reproducible journals (though completion order varies).
- **Local vs remote.** Identical execution. `--local` skips all server calls and writes only local artifacts. Remote mode additionally opens a run, streams results in chunks of 100, and completes the run.

## 5. Calibration — why a judge is not trusted by default

An LLM judge is a *measuring instrument*, and an uncalibrated instrument produces numbers, not measurements. Specific failure modes observed across the industry:

- **Self-preference:** a judge scores outputs from its own model family higher.
- **Verbosity bias:** longer answers score higher independent of quality.
- **Position bias:** in pairwise comparison, the first (or last) option wins disproportionately.
- **Leniency drift:** judges cluster on 4/5, compressing the range so real regressions fall below resolution.
- **Rubric drift:** editing a rubric silently redefines the metric, so a "regression" is actually a changed ruler.
- **Injection:** evaluated content instructs the judge.
- **Silent model upgrades:** the provider changes the model behind an alias and every historical number becomes incomparable.

Gating a merge on an unvalidated judge means blocking engineers with a number nobody has checked. Calibration is the control.

### Calibration procedure

1. Build a calibration dataset: ≥100 examples (≥50 per class for classification), human-labelled, deliberately including boundary cases. Label from a written guideline; two annotators on ≥20 % to measure inter-annotator agreement (Cohen's κ).
2. **The human-agreement ceiling.** If two humans agree at κ = 0.6, a judge scoring κ = 0.6 is at the ceiling and further "improvement" is fitting noise. Report human-human agreement alongside judge-human agreement — a judge is never held to a standard the task itself doesn't support.
3. Run the judge version; compute agreement, κ, **false-pass rate** (judge passes what a human failed — the dangerous direction) and **false-fail rate** (judge fails what a human passed — the annoying direction that erodes trust in CI), confusion matrix, per-class breakdown, mean cost, p95 latency.
4. Persist to `evaluator_calibrations` keyed to the evaluator *version*.
5. **Gating policy:** a gate on a judge evaluator with no calibration emits a `WARN: uncalibrated judge` in the CI report. A gate set may set `require_calibration: {min_agreement: 0.8, max_false_pass_rate: 0.05}`, which turns it into a hard error. Recommended default for safety-relevant metrics (unsubscribe, unsupported claims): required.
6. Re-calibrate whenever the rubric, judge model, or judge params change — which is automatic, since all three are in the config hash that mints a new version.

Also supported: comparing two judge models on the same calibration set, and comparing two evaluator versions, so "is the cheaper judge good enough?" is answerable with data.

### Where human review is mandatory (never automate)

- Locking a golden regression dataset built from production traces.
- Any metric with legal/compliance exposure: unsubscribe handling, suppression lists, claims about real people or companies.
- The initial calibration labels themselves (obviously — labelling them with an LLM makes the whole exercise circular).
- Adjudicating a judge-human disagreement.
- Approving a baseline promotion.

### Where an LLM judge is unnecessary (and should be refused)

- Schema validity, placeholder detection, length limits, forbidden strings, tool-order conformance, cost/latency/token limits, exact-match classification against ground truth, duplicate detection, citation *existence* (as opposed to citation *support*).

Every one of these is deterministic, free, instant, and 100 % reliable. The docs will state the rule plainly: **reach for a judge only when the property is genuinely subjective and no deterministic proxy exists.** A suite where most metrics are judges is a design smell, and the CLI will say so (`hint: 7/8 evaluators are LLM judges; consider deterministic checks for schema/placeholder/length`).

## 6. Aggregation & comparison

Per metric: `mean`, `count`, `stddev`, bootstrap 95 % CI, plus `p50/p95/p99` for operational metrics, and slices by any `metadata` key declared in `slice_by`.

**Significance.** Report a bootstrap CI on the delta and a `significant` boolean (CI excludes 0). Deliberately *not* used to auto-gate: with n = 200, most real regressions are not "significant", and gating on p-values would let real regressions through. Gates use thresholds; significance is advisory context for the human reading the report. Multiple-comparison correction is noted in the report when >10 metrics are compared, again advisory.

**Comparison rules.**
- Match examples by `external_id`, not ordinal — datasets change.
- Refuse to compare runs whose `dataset_content_hash` differs unless `--allow-dataset-mismatch`, and always mark it in the report.
- Refuse to compare a metric whose `evaluator_version` differs, unless `--allow-evaluator-drift`; a changed evaluator means the ruler changed, and reporting that as a quality delta is the single most misleading thing this system could do.
- Per-example regression list: examples that passed in baseline and failed in candidate, sorted by score delta, capped at 50 in the terminal and complete in the JSON.

## 7. Gate engine

```python
@dataclass(frozen=True)
class GateRule:
    metric_key: str
    minimum: float | None = None
    maximum: float | None = None
    max_absolute_regression: float | None = None
    max_relative_regression: float | None = None
    blocking: bool = True
    slice: dict[str, str] | None = None
    require_baseline: bool = False
```

Evaluation order per rule: metric missing → `error` (a typo'd metric key must never silently pass — this is a real trap in comparable tools); evaluator error rate > threshold → `error`; then absolute thresholds; then, if a baseline exists, regression thresholds. Verdict = worst across rules; `fail` on any blocking failure, else `warn`, else `pass`. Exit code: 0 pass/warn, 1 blocking fail, 2 execution error, 3 configuration error.

**Why aggregate averages hide critical failures.** Concrete: a reply-intent classifier over 1 200 examples where `unsubscribe` is 1 % of traffic. A change breaks unsubscribe detection completely — recall 0.99 → 0.20. Overall accuracy falls by `prevalence × recall_drop` = 0.01 × 0.79 = **0.79 percentage points**, from 0.941 to 0.933 — comfortably inside any `max_regression: 0.02` gate. The system now ignores unsubscribe requests, which is a CAN-SPAM/GDPR violation and an actual legal liability, and every gate is green.

Be precise about the arithmetic, because it determines the mitigation. The aggregate is not *uniformly* blind: at 3 % prevalence the same collapse costs 2.4 points and a 2-point gate would catch it. The aggregate is blind **below a prevalence threshold set by the gate tolerance** — and that threshold is invisible to whoever writes the gate. You cannot pick a tolerance that protects every rare class without blocking on ordinary noise in the common ones. That is why the answer is a separate absolute floor on the rare class, not a tighter aggregate tolerance.

The mitigations are structural, not advisory:
1. **Sliced gates** — `applies_to_slice: {class: unsubscribe}` gates the rare class directly.
2. **Protected metrics** — `blocking: true, minimum: 0.98`, absolute floors that ignore the baseline entirely.
3. **Zero-tolerance counts** — `unsupported_claim_rate: {maximum: 0}` gates a count, not a rate, so one violation in a million fails.
4. **Per-class report** — classification evaluators always emit per-class recall, so the number is at least *visible* even when nobody gated it.
5. The suite linter warns when a classification metric is gated only in aggregate.

Rare-and-severe is exactly the class of failure averages are worst at, and it is exactly the class that matters most.
