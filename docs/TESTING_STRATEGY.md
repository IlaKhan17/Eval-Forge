# EvalForge — Testing Strategy

A platform that tells other teams their software is broken has to be conspicuously well tested. Two ideas govern everything below.

**Determinism.** No test may call a real model provider. Judges are exercised through a `FakeModelClient` implementing the `ModelClient` protocol with scripted responses. Real-provider tests live in a separate, manually-triggered, cost-tracked job (`nightly-live`) and never gate a PR. A flaky test suite in a tool that gates other people's CI destroys the product's credibility.

**Eat our own dog food.** The judges and rubrics EvalForge ships are themselves calibrated with EvalForge, and results are committed to the repo.

## 1. Pyramid and targets

| Layer | Count (v0.1) | Runtime | Gate |
|---|---|---|---|
| Unit | ~600 | < 60 s | every PR |
| Integration (testcontainers) | ~120 | < 5 min | every PR |
| Contract | ~50 | < 60 s | every PR |
| E2E | ~12 | < 10 min | every PR (merge queue) |
| Security | ~80 | < 3 min | every PR |
| Load | 5 scenarios | ~10 min | nightly + pre-release |
| Live-provider | ~15 | variable | nightly, non-gating |

Coverage: ≥90 % on `evaluation-core` and `trajectory-engine` (pure, no excuse for less), ≥80 % on `api`/`worker`/`sdk`, no floor on `web` (rely on E2E + a handful of component tests). Coverage is a floor for the pure packages and explicitly *not* a target elsewhere — chasing coverage on I/O code produces mock-heavy tests that assert implementation.

## 2. Unit tests

`pytest`, `pytest-asyncio`, `hypothesis` for property tests, `freezegun` for time, `syrupy` for snapshots.

**Aggregation & metrics.** Mean/percentile correctness against hand-computed fixtures; bootstrap CI reproducibility under a fixed seed; empty-input handling; that errored evaluations are excluded from means and counted separately; macro vs micro F1 on a known confusion matrix; NDCG/MRR/P@K against published worked examples (these are easy to get subtly wrong and are verified against textbook values, not our own implementation).

**Gate engine.** Every rule type × (baseline present / absent); the missing-metric case → `error` not `pass`; blocking vs warning verdict precedence; slice-scoped rules; the **hidden-regression scenario** from `EVALUATION_ENGINE.md` §7 as a named regression test (macro accuracy passes, unsubscribe slice fails, run exits 1); exit-code mapping.

**Policy parsing.** Valid/invalid YAML; unknown rule kind; circular aliases; unknown action name → warning with suggestions; line numbers preserved in errors; content hash stability across formatting-only changes.

**Trajectory normalization** — the highest-value unit suite in the repo, one fixture per rule in `TRAJECTORY_POLICIES.md` §4: ordering by start time, tie-breaking, nesting/flattening, retry collapse (and that retries don't count toward `max_calls` but do toward `unique_action`), parallel groups and conservative `forbidden_before`, failed spans producing events, orphans, clock skew, incomplete traces → `inconclusive` for `required_*` but live evaluation for `forbidden_*`, empty trajectory, duplicate span ids.

**Redaction.** The credential corpus in all four modes; nested structures, lists, non-UTF8 bytes; that redaction is idempotent; that `redaction_count` is accurate; hypothesis property: *for all generated payloads, no substring matching a known credential pattern survives*.

**Cost calculation.** Per-provider token→cost tables; `Decimal` throughout (a property test asserts no float ever enters a cost path); unknown model → cost `None`, not 0 (silently costing zero is worse than admitting ignorance).

**Dataset locking.** Write-after-lock → 409; hash stability under key reordering and whitespace; hash changes on any content change; empty-version lock rejected; clone lineage.

**Experiment comparison.** Matching by `external_id` including added/removed examples; dataset-hash mismatch flagged; evaluator-version drift refused; regression list ordering.

**Suite config.** Env interpolation with/without defaults; missing var → error; `extends` merge semantics; `--set` override typing; every semantic validation rule; that secrets are not persisted into stored config.

## 3. Integration tests

`testcontainers` for Postgres 17, Redis 8, MinIO. Function-scoped transactional rollback for speed; a session-scoped container.

- **Migrations:** upgrade → downgrade → upgrade on a scratch DB; a check that models and migration head are in sync (autogenerate produces an empty diff) — this catches the single most common Alembic mistake.
- **Ingestion:** batch write; idempotent replay (same batch twice → `duplicate_spans` reported, one row); out-of-order arrival (child before parent); oversize → S3 offload + `payload_ref`; content-addressed dedupe (identical payloads → one object); partial acceptance; concurrent ingest of the same trace from two workers.
- **Queue:** enqueue/consume; job retry with backoff; poison job → DLQ after N attempts; worker killed mid-job (SIGKILL) → job re-delivered and completes exactly once at the effect level; graceful shutdown drains.
- **Object storage:** put/get/presign; MinIO unavailable → ingestion degrades to `metadata_only` for that request rather than 500ing; retention sweeper deletes object then row; orphan object tolerated.
- **Experiment lifecycle:** create → run → chunked results → complete → aggregates computed → compare; cancellation mid-run leaves `cancelled` with partial results; resume skips completed examples.
- **Auth:** key hashing/lookup; revocation takes effect within the cache TTL; expiry; scope enforcement; rate limiting under concurrency.

## 4. Contract tests

The seams where drift is silent and expensive.

- **SDK↔API:** the SDK's request models validated against the server's OpenAPI schema; a recorded-fixture suite replays real SDK output against the API and vice versa; forward compatibility (server sends an unknown `span_type` → SDK degrades to `custom` rather than raising).
- **CLI↔API:** every CLI command exercised against a live test API; `evalforge-report.json` validated against its published JSON Schema on every generation.
- **Local↔server parity (the critical one):** golden `(policy, trace) → failures` and `(results, gates) → verdict` fixtures evaluated by both the library and the API; byte-equality asserted on the normalised report. This is what guarantees the CI exit code matches the dashboard.

  **Implemented** as `apps/api/tests/test_parity.py` over `tests/fixtures/parity/` — 8 gate cases and 6 trajectory cases, fewer than the ~70 originally planned but each chosen for a place the two paths *could* diverge rather than to hit a number. It found two real divergences on its first run: the API's wire model for a gate rule had no representation for `severity` or `max_error_rate`, so a `warn` rule arrived as blocking and a rule's error tolerance silently reverted to the default. There is also a structural test asserting every `GateRule` field is representable on the wire, so the next omission fails at the schema rather than waiting for a fixture to happen to exercise it.
- **OTLP/OpenInference (v0.2):** canonical OTLP payloads from real OTel SDKs map to expected span rows; unmapped attributes are preserved losslessly; a round-trip property test.
- **GitHub report format:** rendered markdown snapshot-tested; length within GitHub's comment limit (with a truncation path tested at the boundary).

## 5. End-to-end

Playwright (UI) + a Python driver (API/CLI) against a full `docker compose` stack.

> **Status.** E2E-1 exists as `tests/e2e/test_acceptance.py`, runs against a live server in a
> subprocess, and gates the merge queue. It now covers publishing too: both runs reach the server,
> each records the dataset content hash it ran against, and the corpus metric the gate failed on
> survives the trip — which is the assertion that would have caught the server reading ERROR on a
> run the CLI passed.
>
> The other scenarios listed below — annotate → promote, offline spooling, dataset immutability
> through the UI, the calibration warning in CI — are not written yet.

**E2E-1, the MVP acceptance test** — the entire 14-step loop as one automated scenario: create org/project → issue key → run an instrumented sample app → assert the trace and its nested spans render in the explorer → create and lock a dataset → define deterministic + fake-judge evaluators → `evalforge eval` → assert exit 0 → introduce a seeded regression → re-run → assert exit 1, the correct blocking metric named, and the PR-comment markdown correct. **This test is the definition of done for the MVP**, and it is written in Phase 1 against a stub and progressively un-stubbed each phase — so "does the loop work end to end" is answered continuously rather than in Phase 12.

Others: trajectory violation surfaces with the correct span link; annotate a trace → promote to dataset → appears in the next run; offline mode (API down mid-run → app unaffected, spans spooled, replayed on recovery); dataset immutability enforced through the UI; calibration run produces a stored report and an uncalibrated-judge warning in CI.

## 6. Security tests

As enumerated in `SECURITY.md` §12. Structural point: the cross-tenant and authorization suites are **registry-driven** — they enumerate FastAPI's route table, and any route not explicitly listed as covered or exempt fails the build. Otherwise these suites silently stop covering new endpoints, which is exactly when they're needed.

## 7. Evaluation-science tests

Meta-tests that the evaluation logic is statistically sound:

- Known-answer datasets where the correct metric value is computable by hand.
- **Judge stability:** the same input scored 10× at temperature 0 with the fake client returning realistic variance; assert variance is reported, not hidden.
- **Calibration math** against a hand-built confusion matrix, including Cohen's κ (verified against a published worked example).
- **Adversarial rubric test:** a deliberately vague rubric should produce low human agreement — asserting the calibration machinery *detects* a bad rubric rather than blessing it.
- **Position-bias test:** a fake judge with a hard-coded first-position preference must be caught by the order-swapping logic.

## 8. Load tests

`locust` (HTTP) + a custom span generator. MVP targets — deliberately modest, sized to a self-hosted single-node deployment on 4 vCPU / 8 GiB, not to a hyperscale fantasy:

| Scenario | Target | Notes |
|---|---|---|
| Ingestion throughput | **2 000 spans/s** sustained, p95 < 200 ms | ~50 batches/s of 40 spans |
| Ingestion burst | 10 000 spans/s for 30 s without loss | Absorbed by buffer + queue |
| Spans per trace | 10 000 supported; 500 typical; render < 2 s | Above 10 000 → `413` |
| Trace list query | p95 < 300 ms at 10 M spans | Keyset pagination |
| Trace detail (500 spans) | p95 < 500 ms | |
| Concurrent experiments | 20 concurrent runs × 200 examples | |
| Worker throughput | 500 deterministic evaluations/s/worker | |
| Experiment scheduling latency | < 2 s enqueue→start | |
| Worker cold start | < 10 s | |
| Dashboard TTI | < 2.5 s on a seeded 1 M-span DB | |

Future-scale assumption (v1): 50 000 spans/s, 1 B spans retained. That is the point at which the analytics-store migration in ADR-006 triggers — and stating the trigger now is what keeps us from building for it today.

Every load run records p50/p95/p99, error rate, DB connections, queue depth, and CPU/RSS, and results are committed so regressions are visible over time.

## 9. CI stages

```
┌ pre-commit ── ruff format/check · biome · gitleaks · yaml/json schema lint
│
├ Stage 1 (~2 min, parallel)   lint · mypy --strict (packages) · tsc · import-linter
│                              · "no davis/adaptquiz in apps|packages" check
├ Stage 2 (~2 min)             unit tests (matrix: py3.12) + coverage gate
├ Stage 3 (~5 min)             integration (testcontainers) + migration up/down/up
├ Stage 4 (~2 min)             contract + report-schema validation
├ Stage 5 (~3 min)             security suite + bandit/semgrep/pip-audit/trivy
├ Stage 6 (~10 min)            E2E on docker compose  [merge queue only]
└ nightly                      load · live-provider evals · dependency audit
                               · self-calibration of shipped judges
```

Fail fast: stages are ordered by cost, and a lint failure never waits on containers. Merge queue runs 1–6; a PR runs 1–5 by default with E2E available via a label. Required checks: 1–5. Flaky-test policy: a test that fails twice in a fortnight is quarantined within 24 h with a tracking issue — never re-run until green.
