# Architecture Decision Records

Format per record: Decision · Context · Options · Chosen · Reasoning · Consequences · Future migration path.

---

## ADR-001 — Monorepo structure

**Decision.** Single git repository; `uv` workspace for Python, pnpm workspace for TypeScript; no Turborepo/Nx in the MVP.

**Context.** SDK, CLI, pure libraries, API, worker, and dashboard must version together during rapid iteration. The SDK is published to PyPI; everything else is deployed.

**Options.** (a) Polyrepo. (b) Monorepo, no build orchestrator. (c) Monorepo + Turborepo/Nx. (d) Monorepo + Bazel.

**Chosen.** (b), with the structure below.

```
proofstep/
├── apps/{api,worker,web}/
├── packages/{shared-types,evaluation-core,trajectory-engine,telemetry,python-sdk,cli}/
├── infra/{docker,otel,migrations}/
├── examples/{basic-llm,langgraph-agent,davis-sdr,adaptquiz}/
├── evals/{fixtures,rubrics,policies,suites}/
├── docs/  scripts/  tests/{e2e,load,security}/
└── pyproject.toml  uv.lock  pnpm-workspace.yaml  docker-compose.yml  Makefile
```

**Changes from the proposed layout, with reasons.**
1. `apps/worker` shares a package and image with `apps/api` (same code, different entrypoint) — eliminates version skew and halves build time. It stays a separate directory only for its entrypoint and job modules.
2. `packages/telemetry` merges into `packages/python-sdk`. Two packages with one consumer and no independent release story is speculative modularity; the internal module boundary (`sdk/_telemetry/`) captures the same separation at zero cost. Split it out only if a TypeScript SDK ever needs a shared spec.
3. `shared-types` is Python (pydantic) and **generates** TS types into `apps/web/src/types/generated.ts` via the OpenAPI schema, committed and CI-verified. A hand-maintained cross-language type package drifts.
4. Top-level `tests/` holds only cross-cutting suites (E2E, load, security); unit tests live beside their package.
5. Added `evals/calibration/` — the calibration datasets for Proofstep's own shipped judges.

**Reasoning.** Turborepo's caching matters at ~10+ TS packages; we have one. Bazel is an infrastructure project. The polyrepo tax (coordinated releases across six repos) is real and immediate.

**Consequences.** Simple, cheap CI; contributors clone one thing. Full CI runs on every change until path filters are added (a 30-line change when it hurts).

**Migration path.** Add Turborepo if the web app splits into multiple packages; extract the SDK to its own repo only if its release cadence genuinely diverges.

---

## ADR-002 — Python package manager

**Decision.** `uv` with a workspace, `uv.lock` committed, Python pinned to 3.12.

**Options.** uv · Poetry · PDM · pip-tools.

**Chosen.** uv.

**Reasoning.** Already installed on the dev machine (0.8.22). It is the only option that handles interpreter installation, virtualenvs, workspaces, locking, and tool execution in one binary — Poetry needs pyenv alongside it. 10–100× faster resolution matters most in CI, where installs happen on every job. Native workspace support with path dependencies covers the six-package layout directly. Poetry's non-standard `[tool.poetry]` metadata has historically lagged PEP standards; uv is PEP 621 native, so packages stay portable if we switch. PDM is fine but has a smaller ecosystem; pip-tools does not do workspaces.

**Why 3.12 and not the host's 3.14:** wheel availability across the OTel contrib ecosystem, `psycopg`, and `pydantic-core` is uneven on 3.14. A platform selling reproducibility cannot have a fragile install. `requires-python = ">=3.12,<3.14"`; the SDK alone widens to `>=3.10` since it must install into *users'* applications, and forcing an interpreter upgrade to adopt a tracing library is a non-starter.

**Consequences.** Contributors need uv (one curl command; CI uses `astral-sh/setup-uv`). uv is young — mitigated by the lockfile being a documented format and PEP 621 metadata keeping migration cheap.

**Migration path.** `uv` → Poetry/PDM is a metadata translation, not a rewrite, because dependencies live in standard `[project]` tables.

---

## ADR-003 — Authentication

**Decision.** Self-managed auth: argon2id passwords + short-lived JWT access tokens + rotating refresh cookies for humans; SHA-256-hashed, prefixed, scoped API keys for machines.

**Options.** Supabase Auth · Auth.js · self-managed JWT · Clerk/WorkOS.

**Chosen.** Self-managed.

**Reasoning.** API keys are required regardless — SDK, CLI, and CI cannot use a browser flow — so a hosted auth vendor removes maybe 30 % of the auth surface while adding a hard dependency. Self-hosting is a first-class requirement, and Supabase Auth means either running Supabase or depending on a SaaS in an air-gapped install; that alone disqualifies it. Auth.js is a Next.js library and would put session authority in the frontend while the Python API remains the real trust boundary — the wrong shape. The remaining scope (register, login, refresh rotation with reuse detection, password reset) is well-trodden and roughly 400 lines with `argon2-cffi` and `pyjwt`.

**Note on key hashing:** SHA-256, not bcrypt/argon2, for API keys. They are 256-bit random secrets, not user-chosen passwords, so a slow KDF buys nothing against brute force while adding ~100 ms to every ingest request. Passwords use argon2id.

**Consequences.** We own password reset, lockout, and rotation, and their tests. No vendor lock-in, no per-MAU cost, works offline.

**Migration path.** OIDC/SSO in v1 as an additional identity source behind the same session layer; the `users` table already permits a null `password_hash`.

---

## ADR-004 — Job queue

**Decision.** ARQ.

**Options.** ARQ · Celery · Dramatiq · RQ · Postgres-backed (`pgqueuer`/SKIP LOCKED) · Temporal.

**Chosen.** ARQ.

**Reasoning.** The API is async FastAPI; ARQ is asyncio-native, so jobs share the same async DB session and HTTP client patterns as request handlers — Celery's sync worker model would mean two concurrency paradigms in one codebase. Redis is already a dependency for rate limiting, caching, and locks, so ARQ adds no new infrastructure. Job volume is modest (experiment runs, online eval, rollups, retention), and Celery's broker abstractions, routing, and chord/canvas machinery are unused weight with a well-known operational burden. Temporal would be excellent for durable experiment execution but is a whole additional service to self-host — clearly disproportionate at MVP.

**Consequences.** Redis is a single point of failure for background work (acceptable: ingestion and reads still function; jobs queue up). ARQ has a smaller community and a thinner admin UI — mitigated by a `/v1/queues` endpoint and Prometheus metrics. At-least-once delivery means every job must be idempotent, which is enforced by design (natural keys, `ON CONFLICT`) and tested.

**Migration path.** Job functions are plain async callables behind a thin `enqueue()` façade; swapping to Temporal or Celery touches the façade and the worker entrypoint.

---

## ADR-005 — Trace storage

**Decision.** PostgreSQL with JSONB, tables declared `PARTITION BY RANGE (started_at)` from the first migration. No ClickHouse in the MVP.

**Options.** Postgres only · Postgres + ClickHouse · ClickHouse only · Postgres + TimescaleDB.

**Chosen.** Postgres only, partition-ready.

**Reasoning.** Sizing the actual requirement: 2 000 spans/s sustained is ~170 M spans/day, but the *realistic* MVP load for a self-hoster or a portfolio deployment is 10²–10⁴ spans/minute. At ~2 KB/span with payloads offloaded to S3, 30-day retention at 10 M spans/day is ~600 GB — comfortable for a single Postgres with monthly partitions. Against that, ClickHouse means a second database to operate, a second query dialect, dual-write consistency, no transactional joins between traces and experiments (which we need constantly), and a much heavier `docker compose`. The brief asked for a concrete demonstrated need; at MVP volumes there isn't one. Partitioning from day one is the cheap insurance: it makes retention a `DROP PARTITION` instead of a bloating `DELETE`, and it is nearly free now versus a maintenance window later.

**Consequences.** Analytical queries over months of spans will be slow — accepted, because the product's analytics operate on `aggregate_metrics` (small, precomputed), not raw spans.

**Migration path (triggers, stated so the decision is falsifiable):** sustained > 20 000 spans/s, retained spans > 500 M, or trace-list p95 > 1 s despite tuning. Then: introduce a `TraceStore` interface (already the only access path), dual-write spans to ClickHouse, backfill, cut reads over, keep Postgres for relational/transactional entities. Because all trace access is behind the repository, this is additive.

---

## ADR-006 — Payload storage

**Decision.** Hybrid: inline JSONB below 32 KiB, content-addressed S3 objects above.

**Reasoning.** Most spans are small; a single query returning a trace with inline payloads is the fast, simple path. Large payloads (documents, long contexts) would bloat the table, thrash TOAST, and destroy cache locality. Content addressing by SHA-256 deduplicates the repeated system prompt that appears in every span of every trace — a large real saving, not a micro-optimization. The 32 KiB threshold sits below Postgres's ~2 KiB TOAST trigger multiplied by a comfortable margin, and is configurable.

**Consequences.** Two read paths (a helper hides it). S3 outage → payloads unavailable while metadata still works: graceful degradation. Deletion must span two stores; the sweeper deletes the object first, and a bucket lifecycle rule reclaims orphans, so failures are self-healing.

**Migration path.** Threshold is tunable; a backfill job can move inline payloads out if the distribution shifts.

---

## ADR-007 — OTLP support

**Decision.** Native REST ingestion in v0.1; an OTLP/HTTP receiver in v0.2 that translates into the same tables.

**Reasoning.** The critical MVP loop needs one reliable, debuggable ingestion path. Native REST carries Proofstep-specific fields (capture mode, dropped counts, dataset linkage) that OTLP has no home for, and it is inspectable with `curl`. OTLP is genuinely valuable — it is the difference between "install our SDK" and "change one env var" — but it is an *adoption* feature, not a *capability* feature, and shipping it first would mean designing our data model around a spec we don't control.

**Consequences.** Early adopters install our SDK. Two ingestion paths to test from v0.2 (mitigated by the OTLP receiver being a pure translation into the existing service layer, plus round-trip contract tests).

**Migration path.** Ship the receiver plus a Collector config; consider contributing an Proofstep exporter to the Collector's contrib repo.

---

## ADR-008 — OpenInference semantic conventions

**Decision.** Adopt OpenInference attribute *names* (`llm.model_name`, `llm.token_count.*`, `input.value`, `retrieval.documents`, `tool.name`) on our own span model rather than adopting the spec as our schema.

**Reasoning.** Interop for free: tools and exporters already emitting these attributes map cleanly. But our first-class columns (cost, tool args, capture mode, `proofstep.action`) exceed the spec, and hard-binding our schema to an external spec's evolution would make every upstream change a migration. Convergent naming, independent schema.

**Consequences.** A mapping table to maintain (small, table-driven, contract-tested). Unmapped attributes are preserved losslessly in `attributes` JSONB, so nothing is ever dropped.

---

## ADR-009 — Evaluator execution location

**Decision.** The CLI (the user's own process) executes tasks and evaluators for experiments. The server executes only deterministic and judge evaluators over *ingested traces* for online evaluation. The server never executes user task code.

**Reasoning.** The task needs the user's code, dependencies, secrets, and network. Running it server-side means arbitrary code execution, secret custody, and egress control — three hard problems, each larger than the rest of the MVP. It also makes local development and air-gapped self-hosting work identically, which is a stated requirement.

**Consequences.** CI runners bear the compute and provider cost (which is correct — it's their spend and their rate limits). No "run this experiment from the dashboard" button in the MVP.

**Migration path.** A user-hosted runner (a worker deployed in the customer's infra, polling for jobs) gives the dashboard button without us ever executing customer code. This is the intended v0.4 direction, not a server-side sandbox.

---

## ADR-010 — Custom evaluator sandboxing

**Decision.** No server-side execution of user Python in the MVP. `custom_python` runs client-side only; the server stores a `code_ref` for provenance.

**Reasoning.** In-process Python sandboxing is not achievable. `RestrictedPython`, AST filtering, and audit hooks all have documented escapes. Building half a sandbox is worse than building none, because it invites a feature we cannot actually secure. Declining to run the code is the only honest position at this maturity.

**Consequences.** Custom evaluators cannot run in online evaluation. Deterministic built-ins plus the `business_rule` predicate language cover most of what people would otherwise write in Python — a deliberate investment to reduce demand for the unsafe feature.

**Migration path.** In preference order: user-hosted runner (relocates the problem to where the trust already is) → gVisor/Firecracker microVM per evaluation → WASM. Never a filtered-`exec` in the API process.

---

## ADR-011 — Policy language

**Decision.** Constrained YAML with 12 rule kinds, plus a restricted predicate expression for `when:`/`require:` clauses evaluated over a whitelisted AST.

**Options.** Structured YAML · custom DSL · CEL · OPA/Rego · Python callables.

**Chosen.** Structured YAML + restricted predicates.

**Reasoning.** Policies must be reviewed in PR diffs by people who did not write them; YAML is the most reviewable form. A schema gives static validation with line-precise errors, where a DSL fails at run time. Every enumerated Davis and AdaptQuiz case fits the 12 kinds. Rego is powerful and genuinely well-suited to this shape of problem, but adds a runtime dependency, an unfamiliar language, and error messages that cannot point at an offending span — and span-precise attribution is the whole value proposition. CEL is a reasonable middle ground and is the most likely successor if the predicate layer outgrows itself. Python callables have no static validation, no portability, and reintroduce the sandbox problem.

**Consequences.** Some exotic policies will be inexpressible; the escape hatch is a `custom_python` evaluator client-side. YAML verbosity is accepted in exchange for reviewability.

**Migration path.** If `when:` predicates start growing embedded logic, swap the restricted-AST evaluator for CEL — the interface is a single `evaluate(expr, context) -> bool`.

---

## ADR-012 — Dataset and experiment immutability

**Decision.** Dataset versions are immutable once locked (application check + DB trigger + content hash). Experiments and results are append-only.

**Reasoning.** Reproducibility is the product's core claim, and a claim enforced by convention is not enforced. Three layers because the failure is silent: without the content hash, a comparison across mutated data looks perfectly normal and produces a confidently wrong conclusion.

**Consequences.** Users must create a new version to change data, which is friction — mitigated by drafts, cloning, and lineage. Storage grows with versions (examples are small; acceptable).

**Migration path.** Copy-on-write sharing of unchanged examples between versions if storage becomes a real concern.

---

## ADR-013 — Baseline resolution

**Decision.** Default `latest_on_branch(main)`: the most recent successful experiment run for the same `suite_name` on the repo's default branch. Overridable by explicit run id, a promoted `baseline_label`, or a git ref.

**Options.** Explicit id only · latest on branch · pinned promoted baseline · merge-base commit.

**Reasoning.** "Latest on main" matches how engineers think about regression ("did my branch make it worse than main?") and requires no manual curation, so it works on day one. Explicit ids are precise but unusable in a PR workflow. A pinned promoted baseline is the right answer for a mature team and is supported — but as an opt-in, since it requires a curation habit that doesn't exist yet. Merge-base is the most *correct* semantics but demands an experiment run at exactly that commit, which usually won't exist.

**Consequences.** A bad experiment merged to main becomes the baseline and masks a subsequent regression. Mitigations: absolute-threshold gates (`minimum`) don't depend on the baseline at all — which is precisely why protected metrics use absolute floors; baseline age and commit are shown in every report; a baseline older than 30 days emits a warning.

**Migration path.** Add `merge_base` strategy once scheduled baseline runs exist.

---

## ADR-014 — Frontend architecture

**Decision.** Next.js 15 App Router, TypeScript, Tailwind, shadcn/ui, TanStack Query for interactive data, Recharts for charts, server components for initial reads.

**Reasoning.** Trace lists and detail views are read-heavy and benefit from server rendering; filtering and live views need client caching, which TanStack Query provides. shadcn/ui is copy-in components rather than a dependency, which suits a tool that must be forkable. Recharts is sufficient for the handful of chart types needed (a trend line, a bar comparison, a confusion-matrix heatmap); D3 would be over-capable and slower to build with.

**Consequences.** Next.js is a heavier dependency than a Vite SPA and adds a Node runtime to self-hosting. Accepted for SSR and routing ergonomics; the app is fully containerized so self-hosters need no Vercel account.

**Alternative considered.** A Vite SPA served as static files from FastAPI would simplify self-hosting to a single container. This is genuinely tempting and is the fallback if the Node service proves burdensome for self-hosters — it is a routing/build change, not a rewrite, since all data access is via the REST API.

**Special case:** the waterfall/span-tree view is custom SVG/canvas, not a chart library. It needs virtualization for 10 000 spans, and no chart library does this well.

---

## ADR-015 — Multitenancy

**Decision.** Shared database, shared schema, `project_id` on every tenant-scoped table, isolation enforced at the repository layer, with RLS as a Phase-12 backstop.

**Options.** Shared schema · schema-per-tenant · database-per-tenant · RLS from day one.

**Reasoning.** Schema- or DB-per-tenant makes migrations O(tenants) and cross-tenant platform analytics impossible; both are disproportionate for a product whose primary deployment is self-hosted single-tenant. RLS from day one is attractive but adds session-variable plumbing to every connection path and a debugging burden while the schema is still moving; deferring it is safe *because* the ubiquitous `project_id` column makes it a one-migration change, and the test suite provides the coverage in the meantime.

**Consequences.** A missing predicate is a cross-tenant leak. Mitigations are the three layers in `DATABASE_DESIGN.md` §3, of which the registry-driven cross-tenant test suite is the load-bearing one.

---

## ADR-016 — Deployment model

**Decision.** Docker Compose as the reference deployment. Three images: `api`+`worker` (one image, two entrypoints), `web`, plus stock Postgres/Redis/MinIO. No Kubernetes.

**Reasoning.** The primary user is a developer self-hosting or evaluating locally. `docker compose up` working in under two minutes is a real adoption feature; requiring a cluster is an adoption killer. Nothing in the MVP needs orchestration beyond restart policies.

**Consequences.** No horizontal autoscaling out of the box. Documented scaling path: `docker compose up --scale worker=N`, then a Helm chart when someone actually asks.

**Migration path.** Since the images are plain 12-factor containers with env-var config and no local state, a Helm chart is a packaging exercise, not a re-architecture.

---

## ADR-017 — Local vs remote evaluation

**Decision.** Local execution is the default and the only MVP path; the server is a persistence and comparison layer. `--local` disables server interaction entirely.

**Reasoning.** Follows from ADR-009. Additionally it means the tool is useful before a user creates an account — a large adoption advantage — and it makes the whole engine testable without infrastructure.

**Consequences.** Long-running experiments are tied to a developer's terminal or a CI job. Mitigated by result journaling and `--resume`.
