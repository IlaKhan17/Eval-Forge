# Proposed GitHub Backlog

Labels: `epic` · `phase:N` · `area:{core,sdk,cli,api,worker,web,policy,security,infra,docs,examples}` · `type:{feature,bug,chore,test,docs,spike}` · `size:{S,M,L}` · `priority:{p0,p1,p2}` · `blocked` · `good-first-issue`

Each issue is scoped to one focused PR. `→` denotes a blocking dependency.

---

## EPIC-0 · Repository foundation `phase:0`

| # | Issue | Labels | AC |
|---|---|---|---|
| 0.1 | uv workspace root `pyproject.toml`, Python pinned 3.12, six package stubs with metadata only | infra S p0 | `uv sync` succeeds; `uv.lock` committed |
| 0.2 | pnpm workspace + `apps/web` Next.js skeleton | infra S p0 | `pnpm install && pnpm build` |
| 0.3 | ruff + mypy strict on `packages/` + biome; `Makefile` (`setup/test/lint/dev`) | infra S p0 | `make lint` clean |
| 0.4 | pre-commit: ruff, biome, gitleaks, yaml lint, trailing whitespace | infra S p1 | Blocks a seeded violation |
| 0.5 | `docker-compose.yml`: Postgres 17, Redis 8, MinIO, health checks, 127.0.0.1 binds, random dev creds | infra M p0 | `docker compose up` healthy < 60 s |
| 0.6 | CI stages 1–2 (lint, typecheck, unit) | infra M p0 | Green on empty suite |
| 0.7 | import-linter contract: core/engine may not import http/db | infra S p0 → 0.1 | Seeded violation fails CI |
| 0.8 | Domain-leak check: no `davis`/`adaptquiz` under `apps/`,`packages/` | infra S p1 | Seeded violation fails CI |
| 0.9 | LICENSE (Apache-2.0), README, CONTRIBUTING, CoC, `.env.example` | docs S p1 | |

---

## EPIC-1 · Local evaluation core `phase:1` `area:core`

| # | Issue | Labels | AC |
|---|---|---|---|
| 1.1 | `shared-types`: Example, Score, Metric, Span, Trace, TrajectoryEvent | core S p0 → 0.1 | Models + JSON Schema export |
| 1.2 | `Dataset`: from_jsonl/csv/list/dicts, iteration, filtering, split | core S p0 → 1.1 | Round-trip tests |
| 1.3 | Evaluator/Task/ModelClient protocols + signature-introspection adapter | core M p0 → 1.1 | All four signature forms resolve |
| 1.4 | Deterministic evaluators (9) | core M p0 → 1.3 | Per-evaluator unit tests |
| 1.5 | `business_rule` restricted-predicate evaluator | core M p1 → 1.4 | AST rejects calls/imports/attr traversal |
| 1.6 | Statistical/corpus evaluators + `CorpusEvaluator` protocol | core M p0 → 1.3 | NDCG/MRR/P@K vs. textbook values |
| 1.7 | Async runner: TaskGroup, dual semaphores, timeouts, retries, cancellation | core L p0 → 1.3 | Cancellation finalizes partial run; exit 130 |
| 1.8 | Result journaling + `--resume` | core M p1 → 1.7 | Kill at 190/200, resume completes |
| 1.9 | `FakeModelClient` + judge harness (rubric/classify/binary/pairwise) | core M p0 → 1.3 | Structured output enforced |
| 1.10 | Judge safeguards: input allow-list, delimiters, canary, range validation | core M p0 → 1.9 `area:security` | Injection corpus flagged not scored |
| 1.11 | Pairwise position-bias control (dual-order + tie) | core S p1 → 1.9 | Biased fake judge is detected |
| 1.12 | Operational evaluators from trace | core S p1 → 1.1 | |
| 1.13 | Aggregation: mean, percentiles, stddev, bootstrap CI, slices | core M p0 → 1.6 | Seeded bootstrap reproducible |
| 1.14 | Error/score separation: errors excluded from means, counted | core S p0 → 1.13 | Errored judge ≠ score 0 |
| 1.15 | Gate engine: all rule types, slices, verdict precedence, exit codes | core L p0 → 1.13 | Missing metric → error |
| 1.16 | **Hidden-regression regression test** (macro passes, slice fails) | test S p0 → 1.15 | Named test, exits 1 |
| 1.17 | Comparison: external_id matching, hash mismatch, evaluator drift | core M p0 → 1.13 | Drift refused by default |
| 1.18 | `EvalResult` + JSON report v1 + JSON Schema | core M p0 → 1.15 | Report validates |
| 1.19 | `examples/basic-llm` | examples S p1 → 1.18 `good-first-issue` | Runs offline with the fake client |
| 1.20 | E2E-1 skeleton against local stub | test M p0 → 1.18 | Passes; un-stubbed each phase |

---

## EPIC-2 · Trajectory policy engine `phase:2` `area:policy`

| # | Issue | Labels | AC |
|---|---|---|---|
| 2.1 | **Golden fixture corpus first**: ~40 (policy, trace) → expected failures | test M p0 → 1.1 | Committed before matchers exist |
| 2.2 | Policy JSON Schema + parser with line-preserving errors | policy M p0 → 2.1 | Errors carry line/col |
| 2.3 | Semantic validation: alias cycles, unknown kinds, unknown-action warning | policy S p0 → 2.2 | Unknown action suggests known ones |
| 2.4 | Normalizer: ordering, tie-break, flattening, depth | policy L p0 → 2.1 | One test per §4 rule |
| 2.5 | Normalizer: retry detection + counting semantics | policy M p0 → 2.4 | Retries excluded from `max_calls`, included in `unique_action` |
| 2.6 | Normalizer: parallel groups + conservative `forbidden_before` | policy M p0 → 2.4 | Overlap → no violation |
| 2.7 | Normalizer: incomplete traces → `inconclusive` for `required_*` only | policy M p0 → 2.4 | Asymmetry tested |
| 2.8 | Restricted predicate evaluator (shared with 1.5) | policy M p0 → 1.5 | |
| 2.9 | Matchers: order, required, forbidden, before/after | policy M p0 → 2.4 | |
| 2.10 | Matchers: limit, unique_action, no_loop, max_retries | policy M p0 → 2.4 | |
| 2.11 | Matchers: argument_condition, conditional, final_state | policy M p0 → 2.8 | |
| 2.12 | `PolicyFailure` + message formatter (span id, event index, policy line) | policy M p0 → 2.9 | Snapshot-tested; banned generic messages |
| 2.13 | `trajectory` evaluator adapter in core | core S p0 → 2.12 | |
| 2.14 | Davis + AdaptQuiz example policies as fixtures | examples S p1 → 2.12 | |

---

## EPIC-3 · Python tracing SDK `phase:3` `area:sdk`

| # | Issue | Labels | AC |
|---|---|---|---|
| 3.1 | `init()`, config precedence (args > env > file), `enabled` kill switch | sdk S p0 | |
| 3.2 | Span/Trace objects, contextvar stack, `current_*` | sdk M p0 → 1.1 | |
| 3.3 | Decorators `@trace`/`@span`/`@tool`, sync+async | sdk M p0 → 3.2 | Traceback preserved on raise |
| 3.4 | Context managers + `set_metadata`/`set_state`/`record_event` | sdk S p0 → 3.2 | |
| 3.5 | Propagation: gather/TaskGroup/threads/W3C traceparent | sdk M p0 → 3.2 | Matrix over spawning primitives |
| 3.6 | Redaction pipeline + default deny-list + entropy detection | sdk L p0 `area:security` | 30-credential corpus, 4 modes |
| 3.7 | Capture modes + most-restrictive resolution | sdk M p0 → 3.6 | |
| 3.8 | Bounded buffer, drop-oldest, dropped counter | sdk M p0 → 3.2 | |
| 3.9 | Batching exporter, gzip, backoff+jitter, once-per-window logging | sdk M p0 → 3.8 | |
| 3.10 | Disk spool + replay | sdk M p1 → 3.9 | |
| 3.11 | **Never-raise wrapper on every public entry point** | sdk M p0 | Fault-injection test |
| 3.12 | Deterministic head sampling + always-sample-on-error | sdk S p1 → 3.2 | |
| 3.13 | Payload caps + truncation markers | sdk S p0 → 3.6 | |
| 3.14 | Overhead benchmark < 5 ms p99, API down | test M p0 → 3.11 | Gates CI |
| 3.15 | SDK output → trajectory engine integration test | test S p0 → 2.13, 3.3 | |

---

## EPIC-4a · Schema, auth, projects `phase:4` `area:api`

4a.1 Alembic baseline: orgs/users/memberships/projects/environments (M p0) · 4a.2 **Partitioned** traces/spans/span_events/payload_objects (M p0) · 4a.3 Dataset/evaluator/experiment/policy/gate/CI/annotation tables (L p0) · 4a.4 Migration up/down/up + model-sync CI check (S p0) · 4a.5 FastAPI app factory, settings, RFC 9457 errors, request ids (M p0) · 4a.6 Repository layer with mandatory `TenantContext` (L p0) · 4a.7 argon2 passwords + JWT + refresh rotation with reuse detection (L p0 `area:security`) · 4a.8 API keys: prefix, SHA-256, scopes, 30 s cache, revocation (M p0 `area:security`) · 4a.9 Permission matrix dependency (M p0) · 4a.10 Audit logging middleware + append-only grants (M p0) · 4a.11 Redis rate limiting + headers (M p0) · 4a.12 HMAC-signed keyset cursors (M p0) · 4a.13 `/healthz`,`/readyz` (S p0) · 4a.14 **Registry-driven cross-tenant test suite** (L p0 `area:security`) · 4a.15 Role×endpoint authorization matrix test (M p0 `area:security`)

## EPIC-4b · Ingestion & trace reads `phase:4`

4b.1 Ingest endpoint, batch validation, partial acceptance (L p0) · 4b.2 Idempotent upsert on natural key (M p0) · 4b.3 Out-of-order/stub-trace handling (M p0) · 4b.4 S3 client + content-addressed offload + dedupe (L p0) · 4b.5 Incremental trace rollups on ingest (M p0) · 4b.6 Server-side redaction backstop + `secret_detected` counter (M p0 `area:security`) · 4b.7 Size/depth/decompression-bomb limits (M p0 `area:security`) · 4b.8 Trace list with all filters + keyset pagination (L p0) · 4b.9 Trace detail + span tree single-query assembly (M p0) · 4b.10 Presigned payload URLs, 60 s, audited (M p0 `area:security`) · 4b.11 NDJSON streaming export (S p1) · 4b.12 Ingestion load test to 2 000 spans/s (M p1 `type:test`)

## EPIC-4c · Dataset/evaluator/experiment APIs `phase:4`

4c.1 Dataset + version CRUD, drafts (M p0) · 4c.2 Bulk example append with locked check (M p0) · 4c.3 **Lock: content hash + DB trigger + idempotent** (L p0) · 4c.4 Clone + lineage (S p1) · 4c.5 JSONL/CSV import-export (M p1) · 4c.6 `promote-from-trace` (M p1) · 4c.7 Evaluator registry + per-type config schema validation (M p0) · 4c.8 Evaluator versioning by config hash (M p0) · 4c.9 Experiment create with full reproducibility tuple (M p0) · 4c.10 Runs + chunked result ingest + idempotency keys (L p0) · 4c.11 Run completion + aggregate rollups (M p0) · 4c.12 Cancel + resume (M p1) · 4c.13 `POST /experiments/compare` incl. dataset-hash and evaluator-drift guards (L p0) · 4c.14 Gate set CRUD + server-side evaluation (M p0) · 4c.15 **Local↔server parity contract test** (L p0 `type:test`) · 4c.16 Policy CRUD + versions + `validate` + `evaluate` (M p0) · 4c.17 `ci_runs`/`ci_reports` (M p0)

---

## EPIC-5 · CLI & gates `phase:5` `area:cli`

5.1 Typer skeleton + config file + keyring (M p0) · 5.2 Suite JSON Schema + loader with line-precise errors (L p0) · 5.3 Semantic validation incl. gate-references-unknown-metric → error (M p0) · 5.4 Env interpolation + secret non-persistence (M p0 `area:security`) · 5.5 `extends`/`include` composition, one level (S p1) · 5.6 `--set` typed overrides (S p1) · 5.7 Baseline resolution strategies (M p0) · 5.8 `proofstep eval` orchestration (L p0) · 5.9 `--local` zero-network mode (M p0) · 5.10 `--dry-run` with cost estimate, zero model calls (M p1) · 5.11 Terminal renderer + snapshot tests + NO_COLOR/ASCII (M p0) · 5.12 JSON report + schema validation on every write (S p0) · 5.13 Exit-code matrix (S p0) · 5.14 `--resume`/`--only-failed`/`--filter`/`--limit` (M p1) · 5.15 `datasets`/`experiments`/`evaluators`/`policies`/`traces` subcommands (L p1) · 5.16 `proofstep doctor` (S p2) · 5.17 Judge-heavy-suite hint (S p2)

## EPIC-6 · Dashboard `phase:6` `area:web`

6.1 App shell, auth pages, project switcher (L p0) · 6.2 Generated TS types from OpenAPI + CI drift check (M p0) · 6.3 Trace list + filter bar + keyset pagination (L p0) · 6.4 **Virtualized span waterfall, 10 000 spans** (L p0) · 6.5 Span inspector (M p0) · 6.6 Payload viewer with sanitization + XSS tests (M p0 `area:security`) · 6.7 Evaluation results panel (M p1) · 6.8 Trajectory failure display with span deep-links (M p0) · 6.9 Dataset browser + version diff (M p1) · 6.10 Experiment list + comparison view (L p0) · 6.11 CI run history (S p1) · 6.12 CSP + security headers (S p0 `area:security`) · 6.13 Playwright suite (L p0 `type:test`)

## EPIC-7 · Calibration `phase:7`

7.1 Calibration metrics module incl. Cohen's κ (M p0) · 7.2 Calibration job + API + storage (M p0) · 7.3 `require_calibration` gate enforcement (M p0) · 7.4 Uncalibrated-judge CI warning (S p0) · 7.5 Human-human ceiling reporting (S p1) · 7.6 Judge-model and evaluator-version comparison (M p1) · 7.7 Calibration UI (M p1) · 7.8 Adversarial-rubric + position-bias meta-tests (M p0 `type:test`)

## EPIC-8 · Online eval & annotation `phase:8`

8.1 ARQ setup, job registry, DLQ, `/v1/queues` (L p0) · 8.2 Online deterministic+policy tier at 100 % (M p0) · 8.3 Sampled judge tier + failure-triggered escalation (M p0) · 8.4 Per-project cost budget with abort (M p1 `area:security`) · 8.5 Review queues + `SKIP LOCKED` claiming (M p1) · 8.6 Annotation API + UI (L p1) · 8.7 Promote annotated trace → draft example (M p1) · 8.8 Retention sweeper + partition drop (M p0) · 8.9 GDPR erasure by `user_ref` (M p1 `area:security`)

## EPIC-9 · GitHub Actions `phase:9`

9.1 Composite action (M p0) · 9.2 Report → markdown renderer + snapshot (M p0) · 9.3 Comment upsert by marker, edit-in-place (M p0) · 9.4 Comment on failure path (S p0) · 9.5 Artifact upload (S p1) · 9.6 Length truncation at boundary (S p1) · 9.7 Fork-PR secret guidance + example workflows (M p0 `docs area:security`) · 9.8 Demo PR proving red check + comment (S p0 `type:test`)

## EPIC-10 · OTLP `phase:10`

10.1 OTLP/HTTP receiver (L p1) · 10.2 OpenInference mapping table + lossless overflow (M p1) · 10.3 Round-trip contract tests (M p1) · 10.4 Collector config + docs (S p1) · 10.5 `examples/langgraph-agent` (M p1)

## EPIC-11 · Reference integrations `phase:11` `area:examples`

11.1 Davis fixtures + adapter (M p1) · 11.2–11.7 the six Davis suites (M each, p1) · 11.8 AdaptQuiz fixtures + adapter (M p1) · 11.9–11.12 the four AdaptQuiz suites (M each, p1) · 11.13 Calibration sets for every judge-gated metric (L p1) · 11.14 Domain-leak audit (S p0)

## EPIC-12 · Hardening & launch `phase:12`

12.1 Postgres RLS policies + tests (L p1 `area:security`) · 12.2 Full security suite wired into CI (L p0 `area:security`) · 12.3 Load tests vs. all stated targets, results committed (L p1) · 12.4 Graceful degradation: S3 down, Redis down, provider down (M p0) · 12.5 Prometheus metrics + queue observability (M p1) · 12.6 Docs site: quickstart, concepts, reference, self-hosting (XL p0 `docs`) · 12.7 **Evaluation-methodology guide** — when not to use an eval, when not to use a judge, when a human is mandatory (M p0 `docs`) · 12.8 One-command seeded demo (M p0) · 12.9 PyPI release + SLSA provenance (M p1) · 12.10 Security review + threat-model refresh (M p0 `area:security`)

---

## Suggested milestones

- **M1 "Local evals work"** — EPIC-0, EPIC-1 → a usable standalone library
- **M2 "Trajectories work"** — EPIC-2 → the differentiator, provable in isolation
- **M3 "Traces flow"** — EPIC-3, 4a, 4b, EPIC-6 → observability loop
- **M4 "CI gates work"** — EPIC-4c, 5, 9 → **the MVP acceptance point (E2E-1 fully un-stubbed)**
- **M5 "Trustworthy"** — EPIC-7, 8
- **M6 "v0.1 release"** — EPIC-10, 11, 12
