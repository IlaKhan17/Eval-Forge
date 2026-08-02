# Open Questions & Critical Review

## Part 1 — Critical evaluation of the brief

The brief is unusually well-specified. These are the places where I think it is wrong, risky, or asking for too much.

### 1.1 Over-engineering — cut or defer

| Item | Assessment |
|---|---|
| `packages/telemetry` as a separate package | One consumer, no independent release. Merge into the SDK as an internal module. Speculative modularity. |
| 30+ tables in v1 | Roughly 12 are needed for the MVP loop. Create the rest when their feature lands; empty tables invite half-built features. (The *design* should exist now — it does — but not the migration.) |
| `dataset_example_revisions` | Only meaningful for draft edits. Ship in Phase 8 with annotation, not Phase 4. |
| Review queues + assignments + inter-annotator agreement UI | Full annotation workflow is a product of its own. MVP: annotate + promote. |
| `prompt_versions` and `model_configurations` tables | Correct to record for reproducibility, but the brief's framing edges toward prompt management, an explicit non-goal. Keep them as write-mostly reference rows. |
| 9 trace types + 8 evaluator families + 12 policy kinds simultaneously | The types are cheap (enums). The 40+ evaluators are not. Ship deterministic + statistical + judge + trajectory + operational; defer semantic. |
| Semantic/embedding evaluators | Awkward middle ground: less interpretable than deterministic, less capable than judges, adds an embedding dependency and a cache. Defer. |
| GitHub App | The brief already says defer; agreed emphatically. |

### 1.2 Missing from the brief

1. **Cost controls on evaluation itself.** A suite of 500 examples × 6 judges is real money, and a runaway loop is a denial-of-wallet against your own budget. Added: per-run and per-project cost caps, `--dry-run` estimation, cost reporting per metric.
2. **Resumability of eval runs.** Losing a 40-minute, $12 run to a transient 429 is the worst DX in this category of tool. Added: result journaling + `--resume`.
3. **The "same verdict everywhere" guarantee.** The brief asks for local *and* CI *and* server evaluation without requiring they agree. Divergence here silently destroys trust in the gate. Added as an explicit architectural principle with a contract-test corpus.
4. **Error vs. score-zero distinction.** Not mentioned anywhere in the brief; conflating them is the most common way eval numbers become quietly meaningless.
5. **The human-agreement ceiling in calibration.** The brief asks for judge-human agreement but not human-human agreement. Without the ceiling, you cannot tell a bad judge from an ill-defined task.
6. **Dataset-hash and evaluator-version guards on comparison.** The brief describes comparison but never says what happens when the ruler changed. Comparing across evaluator versions is the most misleading output this system could produce.
7. **Statistical illiteracy hazards.** Corpus metrics (F1, NDCG) are not means of per-example scores. Added a distinct `CorpusEvaluator` protocol; treating F1 as a mean is a real, common bug.
8. **Fork-PR secret handling.** The brief asks for GitHub Actions without addressing that PR-triggered evals with production secrets are a supply-chain compromise.
9. **A "when not to use this" document.** Arguably the highest-trust artifact the project can ship. Added as issue 12.7.

### 1.3 Dangerous assumptions in the brief

1. **"LLM-as-a-judge" listed as one evaluator type among many.** It is qualitatively different: non-deterministic, expensive, biased, injectable, and silently version-drifting. Treating it as peer to `exact_match` invites gating merges on unvalidated numbers. Mitigated by mandatory version pinning, calibration, and the injection safeguards.
2. **"Trajectory evaluation" sounds simple; normalization is the hard part.** Retries, parallel calls, nesting, and incomplete traces each have a correct answer that is not obvious. Ambiguity here yields *confidently wrong verdicts* — worse than no verdict. This is why the phase moved earlier and why the fixture corpus is written before the matchers.
3. **"Custom Python evaluator sandboxing" is listed as a security requirement.** It is not solvable in-process. The plan refuses server-side execution rather than shipping a sandbox that appears to work.
4. **"Compare quality, cost, latency, safety against a baseline" assumes a trustworthy baseline exists.** With `latest_on_branch`, a bad merge becomes the baseline and masks the next regression. Mitigated by absolute-floor gates on protected metrics, which ignore the baseline entirely.
5. **Averages as the default aggregate.** The brief asks about this and it is worth restating as the sharpest point in the whole design: a 3 %-prevalence class can go from 0.99 to 0.20 recall while macro accuracy moves 0.3 %. Structural mitigations, not advice, are required.
6. **Assuming the OTel ecosystem will "just work" on any Python.** Wheel coverage drove the 3.12 pin.

### 1.4 Architectural coupling and scaling traps

- **Coupling risk:** if `evaluation-core` learns about HTTP or the database, local mode dies. Enforced by an import-linter contract, not by discipline.
- **Coupling risk:** if the trajectory engine reads spans directly instead of normalized events, every rule inherits normalization bugs.
- **Scaling trap:** unbounded JSONB on `spans`. Capped and offloaded.
- **Scaling trap:** `OFFSET` pagination on traces. Keyset only.
- **Scaling trap:** computing aggregates at read time. Precomputed into `aggregate_metrics`.
- **Scaling trap:** non-partitioned span tables. Partitioned from migration 1 — nearly free now, a maintenance window later.
- **Migration difficulty:** the analytics-store question. Deliberately deferred with a *falsifiable trigger* (ADR-005) so the decision can be revisited on evidence rather than anxiety.

### 1.5 Where ordinary tests beat evals

Schema validity, placeholder detection, length limits, forbidden strings, tool-call ordering for a deterministic orchestrator, cost/token arithmetic, retry logic, JSON parsing, oversized-file rejection, deduplication keys.

Rule shipped in the docs: **if the assertion holds for every input given correct code, write a `pytest` test. If it holds only statistically, write an eval.** Running a 200-example dataset through a judge to discover your serializer emits `null` is slow, expensive, and less reliable than one unit test.

### 1.6 Where an LLM judge is unnecessary

Anything with ground truth (classification accuracy, entity resolution, date extraction), anything mechanically checkable (schema, regex, length, set membership, citation *existence*), anything ordinal with labels (ranking metrics), and anything about the trace rather than the text (order, counts, cost, latency). Approximately 60 % of the metrics across the ten reference suites are non-judge — which is the intended ratio.

### 1.7 Where human review is mandatory

Locking a golden dataset; all calibration labels; any metric with legal exposure (unsubscribe, suppression, claims about real entities, factual correctness in an educational product); adjudicating judge-human disagreements; approving a baseline promotion. These are not defaults to be overridden — they are stated as requirements.

---

## Part 2 — Open questions

Format: Question · Why it matters · Options · **Recommended default** · What would change it.

### Q1 — Is the primary deliverable a hosted product or a self-hosted OSS tool?
**Matters:** drives auth, multitenancy investment, and whether the dashboard is essential or optional.
**Options:** (a) OSS-first, self-hosted; (b) hosted SaaS with OSS core; (c) library only.
**Default: (a).** Every MVP decision assumes it — local-first, no vendor auth, Compose deployment.
**Would change if:** you intend to monetize soon, in which case org/billing boundaries and hosted-runner design should land earlier.

### Q2 — Is EvalForge a portfolio project or a product with external users?
**Matters:** the difference between "impressive and complete" and "supportable". Availability targets, migration discipline, and backwards-compatibility guarantees all hinge on it.
**Options:** (a) portfolio/reference; (b) internal tool for Davis + AdaptQuiz; (c) public OSS with users.
**Default: (b) with (c) aspirations** — build to (c) quality on the critical loop, accept (b) scope elsewhere.
**Would change if:** external users adopt it, at which point API stability becomes a hard constraint.

### Q3 — Which model provider(s) for judges in v0.1?
**Matters:** a `ModelClient` protocol is provider-agnostic, but the shipped rubrics and calibration numbers are provider-specific.
**Options:** (a) one provider; (b) two behind the protocol; (c) LiteLLM-style universal adapter.
**Default: (b)** — the protocol plus two implementations, so the abstraction is validated by a second case rather than assumed.
**Would change if:** self-hosters demand local models, making an OpenAI-compatible endpoint adapter the priority (it is cheap; add it early if asked).

### Q4 — Baseline: latest-on-main or pinned promoted baseline?
Covered in ADR-013. **Default: `latest_on_branch(main)`**, with promotion supported. **Would change if** main becomes unstable, at which point pinned baselines become the recommended default in the docs.

### Q5 — Should quality gates run server-side, client-side, or both?
**Matters:** the CI exit code must work offline; the dashboard must show the same verdict.
**Default: both, from one implementation**, with a golden-fixture contract test asserting equality. **Would change if** the two ever diverge in practice — then client-side becomes authoritative and the server merely records.

### Q6 — How much of Davis/AdaptQuiz gets built in this repo?
**Matters:** the fastest way to corrupt a platform is to let a reference integration's domain concepts leak in.
**Options:** (a) thin adapters + fixtures only; (b) runnable mini-apps; (c) full integration in their own repos.
**Default: (a) in this repo, (c) for real integration.** `examples/` gets fixtures and adapter code sufficient to run the suites offline.
**Would change if** a suite cannot be demonstrated without a working app — then a minimal (b) for that one flow.

### Q7 — Do we support multi-trace (distributed sub-agent) policies in v0.1?
**Matters:** agent systems increasingly spawn sub-agents with linked, not nested, traces.
**Default: no**, documented as a limitation; single-trace only. **Would change if** Davis's architecture spawns sub-agent traces — then span links become a v0.2 requirement, affecting the normalizer.

### Q8 — Dataset size ceiling for v0.1?
**Matters:** determines whether the runner streams or materializes.
**Default: 10 000 examples**, materialized in memory (a 10 000-example dataset at 2 KB each is 20 MB — fine). Streaming is a later optimization. **Would change if** a reference suite needs 100 000+, which none do.

### Q9 — Should the dashboard be optional?
**Matters:** a Node service in `docker compose` is real weight for a CLI-first user.
**Default: yes, optional** — a `--profile web` in Compose. The API and CLI are fully functional without it. **Would change if** the trace explorer becomes the primary adoption surface, in which case it should be default-on.

### Q10 — Retention defaults?
**Default: 30 days traces / 14 days payloads.** Conservative because this data is sensitive (`SECURITY.md` §1). **Would change if** users find it too aggressive — but the safe direction to be wrong in is "too short".

### Q11 — Do we version the suite YAML format separately from the product?
**Matters:** suite files live in users' repos and must not break on upgrade.
**Default: yes** — `apiVersion: evalforge.dev/v1`, with a documented deprecation policy and a migration command when v2 arrives. Cheap now, expensive to retrofit.

### Q12 — Should `evalforge eval` require a locked dataset version?
**Matters:** running against a draft produces an unreproducible experiment.
**Default: require locked; `--allow-draft` marks the experiment `reproducible: false`** and excludes it from baseline eligibility. **Would change if** the friction blocks iteration — but the flag already provides the escape hatch.

### Q13 — Is single-node Postgres availability acceptable?
**Default: yes for v0.1.** Availability target: 99 % for a self-hosted single node, best-effort. Ingestion buffers client-side during downtime, so an outage degrades rather than loses. **Would change if** anyone depends on it for production alerting — an explicit non-goal.

### Q14 — Do we ship a TypeScript SDK?
**Default: no**, until the Python SDK is stable and the wire format has been frozen for a release. The brief agrees. **Would change if** a target integration is Node-only.

### Q15 — How do we prevent judge-model drift from silently invalidating history?
**Matters:** a provider alias silently repointing makes every historical number incomparable — and nothing surfaces it.
**Default:** pin the fully-qualified model version string in the evaluator version; record the provider's returned model id on every judge call and **warn loudly on mismatch**; schedule periodic re-calibration and flag calibrations older than 30 days in the CI report.
**Would change if** a provider stops exposing versioned identifiers, in which case a canary calibration subset should run on every CI invocation to detect drift empirically.

---

## Part 3 — Assumptions made without asking

1. Apache-2.0 licence.
2. Python 3.12 pinned (3.10+ for the SDK); host 3.14 not used for the project.
3. Postgres 17, Redis 8, MinIO for local S3.
4. Trajectory phase moved before the SDK phase.
5. `packages/telemetry` merged into the SDK; `apps/worker` shares an image with `apps/api`.
6. Semantic/embedding evaluators deferred; annotation and online evaluation reduced in scope.
7. Ingestion is REST-first, OTLP in v0.2.
8. Gate evaluation is duplicated locally and server-side from one implementation.
9. `docs/SDK_AND_CLI.md`, `docs/ADR.md`, `docs/REFERENCE_SUITES.md`, `docs/BACKLOG.md`, and `docs/REPOSITORY_ASSESSMENT.md` were added beyond the suggested file list; `docs/DATABASE_DESIGN.md` absorbed the ER diagram rather than splitting it out.

All are cheap to reverse and none blocks the plan.
