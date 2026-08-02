# EvalForge — API Design

FastAPI, Pydantic v2, JSON over HTTPS. Base path `/v1`. OpenAPI 3.1 is generated and published; the Python SDK models and the frontend TS types are both generated from it, so drift is a build failure rather than a bug report.

## 1. Cross-cutting conventions

### 1.1 Authentication

Two credential types, one resolution middleware:

| Credential | Header | Principal | Used by |
|---|---|---|---|
| API key | `Authorization: Bearer ef_<env>_<public>_<secret>` | project + scopes | SDK, CLI, CI |
| Session JWT | `Authorization: Bearer <jwt>` | user + org memberships | Dashboard |

API key resolution: split on `_`, look up by `prefix` (indexed), constant-time compare SHA-256 of the presented key, check `revoked_at`/`expires_at`, cache the resolved principal in Redis for 30 s (bounded so revocation takes effect promptly). Scopes: `ingest`, `read`, `write`. An ingest-only key that calls a control-plane endpoint gets **403** (it's authenticated, just unauthorized); a valid key reaching into another project gets **404** (never confirm existence across a tenant boundary).

JWT: HS256 (single-service) with a rotating secret, 15-minute access token + 30-day refresh token stored as an `HttpOnly; Secure; SameSite=Lax` cookie. Refresh tokens are rotated on use with reuse detection (a replayed refresh token revokes the whole family).

### 1.2 Authorization

Role → permission matrix, checked by a `require(permission)` dependency:

| Permission | owner | admin | developer | reviewer | viewer |
|---|:--:|:--:|:--:|:--:|:--:|
| project.manage / keys.manage | ✅ | ✅ | | | |
| members.manage | ✅ | ✅ | | | |
| dataset.write / lock | ✅ | ✅ | ✅ | | |
| evaluator.write, policy.write, gate.write | ✅ | ✅ | ✅ | | |
| experiment.run / results.write | ✅ | ✅ | ✅ | | |
| annotation.write | ✅ | ✅ | ✅ | ✅ | |
| read (traces, experiments, datasets) | ✅ | ✅ | ✅ | ✅ | ✅ |
| audit.read | ✅ | ✅ | | | |

### 1.3 Error model

RFC 9457 problem details, always:

```json
{
  "type": "https://errors.evalforge.dev/dataset_version_locked",
  "title": "Dataset version is locked",
  "status": 409,
  "detail": "Version v3 of dataset email-quality was locked at 2026-07-11T09:02:11Z and cannot be modified.",
  "instance": "/v1/dataset-versions/018f.../examples",
  "request_id": "01J8K...",
  "errors": [{"field": "examples[3].input", "code": "missing", "message": "field required"}]
}
```

`request_id` is echoed in the `X-Request-Id` response header and appears in server logs — the single most useful thing for supporting a self-hoster.

Status usage: `400` malformed, `401` no/bad credential, `403` authenticated but lacking permission, `404` not found *or* cross-tenant, `409` state conflict (locked, duplicate slug), `413` payload too large, `422` semantically invalid body, `429` rate limited (with `Retry-After`), `499` client cancelled, `5xx` ours.

### 1.4 Pagination

Keyset only. Request: `?limit=50&cursor=<opaque>`. Response:

```json
{"data": [...], "next_cursor": "eyJ0cyI6...", "has_more": true}
```

The cursor is base64 of `{sort_key_value, id}`, HMAC-signed so it cannot be tampered into a cross-tenant scan. `limit` max 200 (1000 for export endpoints, which stream NDJSON instead).

### 1.5 Idempotency

- **Ingestion** is idempotent by natural key `(project_id, trace_id, span_id)` — no header needed, retries are always safe.
- **Mutating control-plane POSTs** accept `Idempotency-Key`. The key + request-body hash + endpoint is stored in Redis for 24 h with the response; a replay with the same key returns the stored response, a replay with a *different* body returns `409 idempotency_key_reuse`.
- Endpoints requiring it: `POST /v1/experiments`, `/results`, `/v1/ci-runs`, `/v1/api-keys`.

### 1.6 Rate limits

Redis token bucket, keyed by API key (or user for JWT). `X-RateLimit-{Limit,Remaining,Reset}` on every response.

| Class | Default |
|---|---|
| Ingestion | 600 req/min, 200 000 spans/min per project |
| Read | 300 req/min |
| Write (control plane) | 60 req/min |
| Auth (login, key issue) | 10 req/min per IP **and** per account |
| Compare / expensive query | 30 req/min |

Limits are project settings so self-hosters can raise them.

### 1.7 Audit

Every state change writes an `audit_logs` row. Mandatory (non-negotiable) for: API key create/revoke, membership change, dataset lock, project settings change, retention change, capture-mode change, data deletion, gate-set change, baseline promotion.

## 2. Endpoints

Notation: `[auth]` = credential type, `[perm]` = permission.

### 2.1 Auth & organizations

```
POST   /v1/auth/register            → 201 {user}                [public, rate-limited]
POST   /v1/auth/login               → 200 {access_token} +cookie [public]
POST   /v1/auth/refresh             → 200 {access_token}         [cookie]
POST   /v1/auth/logout              → 204
GET    /v1/me                       → 200 {user, memberships}    [jwt]
GET    /v1/orgs                     → 200 {data:[org]}           [jwt]
POST   /v1/orgs                     → 201 {org}                  [jwt]
GET    /v1/orgs/{org_id}/members    → 200                        [jwt, members.manage|read]
POST   /v1/orgs/{org_id}/members    → 201  invite                [jwt, members.manage]
PATCH  /v1/orgs/{org_id}/members/{id} → 200 change role          [jwt, members.manage]
```

### 2.2 Projects, environments, keys

```
GET    /v1/projects                          [jwt]
POST   /v1/projects                          [jwt, project.manage]
GET    /v1/projects/{project_id}             [any]
PATCH  /v1/projects/{project_id}             [jwt, project.manage]  ← capture mode, retention, sampling; audited
GET    /v1/projects/{project_id}/environments
POST   /v1/projects/{project_id}/environments
POST   /v1/projects/{project_id}/api-keys    [jwt, keys.manage]
GET    /v1/projects/{project_id}/api-keys    ← never returns secrets, only prefix/metadata
DELETE /v1/api-keys/{key_id}                 [jwt, keys.manage]  revoke; audited
```

`POST /api-keys` request `{name, environment_id?, scopes[], expires_at?}` → `201 {id, prefix, key, scopes, expires_at}` where `key` is **the only time the secret is returned**, with `"warning": "Store this now; it cannot be retrieved again."`

### 2.3 Ingestion

```
POST   /v1/ingest/traces        [api key, scope=ingest]
POST   /v1/ingest/spans         [api key, scope=ingest]
POST   /v1/otlp/v1/traces       [api key, scope=ingest]   ← v0.2
```

`POST /v1/ingest/traces` accepts a batch — this is the SDK's only hot path:

```json
{
  "resource": {"service.name": "davis", "environment": "production",
               "git.commit": "a1b2c3", "sdk.version": "0.1.0"},
  "traces": [{"trace_id": "…32hex", "name": "generate_outreach",
              "started_at": "…", "ended_at": "…", "status": "ok",
              "metadata": {}, "tags": {}}],
  "spans": [{"trace_id": "…", "span_id": "…16hex", "parent_span_id": null,
             "name": "research_prospect", "span_type": "agent",
             "started_at": "…", "ended_at": "…", "status": "ok",
             "attributes": {"llm.model_name": "…", "llm.token_count.total": 1234},
             "input": {...}, "output": {...},
             "events": [{"name": "retry", "timestamp": "…", "attributes": {}}]}],
  "dropped_span_count": 0
}
```

Response `202`:
```json
{"accepted_traces": 1, "accepted_spans": 9, "duplicate_spans": 0,
 "rejected": [{"span_id": "…", "reason": "payload_too_large"}]}
```

Semantics that matter:
- **Partial acceptance.** Valid spans are stored even if a sibling is rejected. Rejecting an entire batch because one span is oversized would make one bad span poison a whole trace.
- **`202`, not `201`** — post-processing (online eval, rollups) is asynchronous.
- `Content-Encoding: gzip` supported and expected.
- Spans arriving for an unknown trace create a stub trace row.
- Spans arriving after retention deletion are accepted and dropped silently (counted in metrics) rather than erroring.

**Should ingestion use separate endpoints or an OTLP receiver?** Both, in that order. Recommendation: ship the native REST endpoint first because (a) it carries EvalForge-specific fields (dataset linkage, capture mode, dropped counts) that OTLP has no place for, (b) it is trivially debuggable with `curl`, (c) OTLP protobuf adds a compile-time dependency and a second serialization path to test. Then add an OTLP/HTTP receiver in v0.2 as a **translation layer that writes the same tables**, so any OpenTelemetry-instrumented app can point at us with a one-line env var. The OTLP path maps OpenInference attributes onto our span columns; unmapped attributes land in `attributes` JSONB losslessly.

### 2.4 Traces (read)

```
GET  /v1/traces?environment=&name=&status=&model=&prompt_version=&git_commit=
                &since=&until=&min_latency_ms=&max_latency_ms=&min_cost=&max_cost=
                &score_evaluator=&min_score=&error_category=&tag.key=value
                &q=&limit=&cursor=
GET  /v1/traces/{trace_id}                      → trace + rollups + evaluation summary
GET  /v1/traces/{trace_id}/spans                → full span tree (single query, assembled server-side)
GET  /v1/spans/{span_id}/payload?field=input    → 302 to a short-lived presigned URL, or inline JSON
GET  /v1/traces/export?…                        → streaming NDJSON  [read]
POST /v1/traces/{trace_id}/annotations          [annotation.write]
GET  /v1/traces/{trace_id}/evaluations
```

`q` is a full-text search over span names, tool names, and error messages — deliberately **not** over payloads (searching model outputs at scale needs an inverted index we're not building in the MVP, and pretending otherwise produces a slow, sometimes-wrong search).

Payload access is via presigned URL with a 60-second TTL, scoped to a single object, and audited when the project's capture mode is `full`.

### 2.5 Datasets

```
POST /v1/datasets                                   {name, slug, kind, description}
GET  /v1/datasets
GET  /v1/datasets/{id}
POST /v1/datasets/{id}/versions                     {version, parent_version_id?, split?, notes?} → draft
GET  /v1/datasets/{id}/versions
POST /v1/dataset-versions/{id}/examples             bulk append (max 1000/req) → 409 if locked
GET  /v1/dataset-versions/{id}/examples             keyset paginated
PATCH /v1/dataset-examples/{id}                     draft only; writes a revision row
DELETE /v1/dataset-examples/{id}                    draft only
POST /v1/dataset-versions/{id}/lock                 → {content_hash, example_count, locked_at}
POST /v1/dataset-versions/{id}/clone                {new_version} → new draft, parent set
GET  /v1/dataset-versions/{id}/export?format=jsonl|csv   streaming
POST /v1/datasets/{id}/import                       multipart jsonl/csv → draft version
POST /v1/dataset-versions/{id}/promote-from-trace   {trace_id, span_id?, input_path, expected}
```

`lock` is the pivotal operation: it computes `content_hash`, sets `status='locked'`, and is idempotent (re-locking returns the same hash, `200`). Locking an empty version is `422` — a silently empty dataset produces a passing experiment, which is the worst possible failure mode.

CSV import is intentionally limited to flat `input.*`/`expected.*`/`metadata.*` column prefixes; anything nested must use JSONL. Guessing at nested CSV semantics creates data-quality bugs that surface as mysterious eval failures.

### 2.6 Evaluators

```
POST /v1/evaluators                        {name, slug, type, description}
GET  /v1/evaluators
POST /v1/evaluators/{id}/versions          {config, judge_model?, judge_params?, output_kind, code_ref?}
GET  /v1/evaluator-versions/{id}
POST /v1/evaluator-versions/{id}/calibrate {calibration_dataset_version_id, judge_model?} → 202 {job_id}
GET  /v1/evaluator-versions/{id}/calibrations
```

Creating a version validates `config` against a per-type JSON Schema and returns `422` with a field path on failure — catching a malformed rubric at registration is far cheaper than at run time.
`type=custom_python` versions accept only a `code_ref` (git path + sha); no code is uploaded or executed server-side (ADR-010).

### 2.7 Experiments

```
POST /v1/experiments                    → 201 {experiment}          [Idempotency-Key]
GET  /v1/experiments?suite=&branch=&is_baseline=&since=
GET  /v1/experiments/{id}
POST /v1/experiments/{id}/runs          → 201 {run}  (client-executed: CLI opens a run)
POST /v1/experiment-runs/{id}/results   bulk append (≤500/req)      [Idempotency-Key]
POST /v1/experiment-runs/{id}/complete  {status, aggregates?}       → finalizes; computes rollups
POST /v1/experiment-runs/{id}/cancel
GET  /v1/experiment-runs/{id}
GET  /v1/experiment-runs/{id}/results?status=&failed_only=
GET  /v1/experiment-runs/{id}/metrics
POST /v1/experiments/compare            {candidate_run_id, baseline_run_id|baseline_selector, gate_set_id?}
POST /v1/experiments/{id}/promote-baseline  {label}                  [experiment.run; audited]
POST /v1/experiments/{id}/run           → 202  server-side execution — **v0.3, not MVP**
```

`POST /experiments/compare` response:

```json
{
  "candidate_run_id": "…", "baseline_run_id": "…",
  "dataset_match": true, "dataset_content_hash": "…",
  "metrics": [
    {"key": "grounded_personalization", "baseline": 0.93, "candidate": 0.91,
     "absolute_delta": -0.02, "relative_delta": -0.0215, "n": 200,
     "ci_low": -0.05, "ci_high": 0.01, "significant": false}
  ],
  "gates": [
    {"metric_key": "unsubscribe_recall", "verdict": "fail", "blocking": true,
     "rule": "minimum", "threshold": 0.98, "actual": 0.74,
     "message": "unsubscribe_recall 0.740 < minimum 0.980"}
  ],
  "verdict": "fail",
  "regressed_examples": [{"external_id": "ex-042", "metric": "…",
                          "baseline_score": 1.0, "candidate_score": 0.0,
                          "trace_id": "…"}]
}
```

`dataset_match: false` when the two runs used different dataset content hashes — the comparison is still returned but flagged, because comparing across datasets is a common and silent source of wrong conclusions. The CLI turns this into a prominent warning; a gate set may declare `require_dataset_match: true` to make it an error.

`baseline_selector` supports `{"strategy": "latest_on_branch", "branch": "main", "suite": "sdr-email-quality"}` (ADR-013).

### 2.8 Policies, gates, CI

```
POST /v1/trajectory-policies                    {name, slug}
POST /v1/trajectory-policies/{id}/versions      {source_yaml} → parsed+validated or 422 with line numbers
POST /v1/trajectory-policies/validate           {source_yaml} → dry-run validation, no persistence
POST /v1/trajectory-policies/{id}/evaluate      {trace_id, policy_version_id?} → failures  (debugging aid)
POST /v1/quality-gate-sets                      {name, source_yaml}
GET  /v1/quality-gate-sets/{id}
POST /v1/ci-runs                                [Idempotency-Key: workflow_run_id]
GET  /v1/ci-runs/{id}
GET  /v1/ci-runs?repository=&pr_number=
POST /v1/ci-runs/{id}/report                    {format, content} → stores + returns permalink
```

`POST /v1/ci-runs` `{provider, repository, pr_number, commit_sha, branch, workflow_run_id, candidate_run_id, baseline_run_id?, gate_set_id}` → `201 {id, verdict, blocking_failures, url}`. Idempotent on `workflow_run_id` so a re-run of the same job updates rather than duplicates.

### 2.9 Annotation & review

```
GET  /v1/review-queues
POST /v1/review-queues                  {name, filter}
GET  /v1/review-queues/{id}/next        → claims the next item (SKIP LOCKED)
POST /v1/review-assignments/{id}/complete {label?, rating?, comment?, correction?}
POST /v1/annotations
GET  /v1/annotations?target_type=&target_id=
```

`/next` uses `SELECT … FOR UPDATE SKIP LOCKED` so concurrent reviewers never receive the same item.

### 2.10 Operational

```
GET /healthz    → 200 always if the process is up  (no dependency checks)
GET /readyz     → 200 iff Postgres + Redis reachable and migrations at head
GET /metrics    → Prometheus (optionally auth-gated)
GET /v1/queues  → queue depths, DLQ size, oldest job age  [admin]
```

## 3. Endpoint specification table (representative)

| Endpoint | Auth | Perm | Idempotent | Paginated | Rate class | Audited |
|---|---|---|---|---|---|---|
| `POST /v1/ingest/traces` | key | ingest | natural key | – | ingest | no (volume) |
| `GET /v1/traces` | key/jwt | read | – | keyset | read | no |
| `GET /v1/spans/{id}/payload` | key/jwt | read | – | – | read | yes if `full` |
| `POST /v1/datasets/{id}/versions` | key/jwt | dataset.write | header | – | write | yes |
| `POST /v1/dataset-versions/{id}/lock` | key/jwt | dataset.lock | intrinsic | – | write | **yes** |
| `POST /v1/experiment-runs/{id}/results` | key/jwt | experiment.run | header | – | write | no |
| `POST /v1/experiments/compare` | key/jwt | read | – | – | expensive | no |
| `POST /v1/experiments/{id}/promote-baseline` | jwt | experiment.run | – | – | write | **yes** |
| `POST /v1/api-keys` | jwt | keys.manage | header | – | auth | **yes** |
| `PATCH /v1/projects/{id}` | jwt | project.manage | – | – | write | **yes** |

## 4. Versioning & compatibility

- URL-versioned (`/v1`). Additive changes only within a major version: new optional fields, new endpoints, new enum values (clients must tolerate unknown enum values — the SDK deserializes unknown `span_type` to `custom` rather than raising).
- Deprecation: `Deprecation` and `Sunset` headers, minimum two minor releases of overlap, warning in the SDK log once per process.
- The SDK sends `X-EvalForge-SDK-Version`; the server rejects SDK majors it cannot parse with a `426` and an actionable message.
