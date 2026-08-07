# Hardening: row-level security and the application role

This is the state of Phase 12. It covers what is **done** — the database-layer tenant isolation
backstop the threat model has been deferring since Phase 2 — and is explicit about what is not.

## The one thing you must do before trusting isolation

```bash
# Once, as a superuser, after migrations.
psql "$ADMIN_URL" -v role_password="$(openssl rand -hex 24)" -f scripts/create_app_role.sql

# Then point the application at it.
POSTGRES_USER=evalforge_app
POSTGRES_PASSWORD=<the password you generated>
```

Check it took effect:

```bash
curl -s localhost:8000/readyz
{"status": "ready", "checks": {"database": "ok", "row_level_security": "enforced (26/26 tables)"}}
```

If instead you see this, the policies exist and do **nothing**:

```json
{"checks": {"row_level_security": "not_enforced"},
 "warnings": ["RLS IS NOT IN EFFECT: role 'evalforge' is a superuser, which is exempt from every RLS policy"]}
```

## Why that warning is the most important line in this document

Postgres exempts superusers and roles with `BYPASSRLS` from every row-level-security policy,
unconditionally. `FORCE ROW LEVEL SECURITY` does not reach them.

So an application connecting as the database superuser has RLS **installed, enabled, forced,
policied, and completely inert** — with nothing in its behaviour to suggest it. This is not
hypothetical: this repository's own `docker compose` role is a superuser, so the first run of these
policies changed nothing at all. The migration was correct and the protection was zero.

Every way RLS stops working shares that shape: a missing policy, `FORCE` left off, a new
tenant-scoped table, an exempt role. Nothing breaks. You simply have no isolation at the database
layer. A security control whose failure mode is "everything keeps working" needs an explicit check,
which is why `verify_enforced` reports the role first and `/readyz` surfaces it.

RLS state is a **warning, not a readiness failure**. An instance whose RLS is bypassed still serves
correct traffic — the repository predicate is layer 1 and does the actual filtering — so refusing
readiness would take a working deployment offline over a defence-in-depth gap.

## The three layers

| Layer | Mechanism | Catches |
|---|---|---|
| 1 | repository injects `project_id` into every query | the normal case |
| 2 | cross-tenant test suite over every endpoint | a missing predicate at review time |
| 3 | **RLS policies** (this phase) | a missing predicate that reached production |

RLS is the backstop for a bug in layer 1, not a substitute for it. That framing decides how it is
built: the tenant it reads is bound once per transaction from the authenticated principal, never
from anything a request body can influence.

## How the policy is written, and why each part is there

```sql
ALTER TABLE traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE traces FORCE ROW LEVEL SECURITY;
CREATE POLICY traces_tenant_isolation ON traces
  USING      (project_id = nullif(current_setting('evalforge.project_id', true), '')::uuid)
  WITH CHECK (project_id = nullif(current_setting('evalforge.project_id', true), '')::uuid);
```

- **`FORCE`** — without it a table's owner is exempt, so a deployment that runs migrations and the
  application as one role is unprotected while appearing protected.
- **`WITH CHECK` as well as `USING`** — `USING` alone filters reads and permits writes, letting a
  caller insert a row into another tenant that it then cannot see. That is a data-corruption bug
  wearing a security bug's clothes.
- **`, true`** on `current_setting` — a *missing* setting returns NULL instead of raising.
- **`nullif(..., '')`** — a *cleared* setting is an empty string, and `''::uuid` raises "invalid
  input syntax". Without this, a connection with no tenant context gets a 500 rather than an empty
  result, which is the opposite of failing closed. The first version behaved exactly that way, and
  the test that caught it is `test_no_tenant_context_sees_nothing`.

The tenant is bound with `SET LOCAL` semantics (`set_config(..., true)`), so it dies with the
transaction. A plain `SET` would persist for the connection's lifetime, and under a connection pool
that is precisely the cross-tenant bug RLS exists to catch.

## The `api_keys` exception

`api_keys` is the one tenant-scoped table with no policy, and the reason is structural:
authentication has to read it to *discover* which tenant a credential belongs to, so a policy keyed
on the tenant would make every request fail. Isolation there comes from the lookup itself — a
globally unique `prefix` and a SHA-256 of the secret, so a row is useless without the token that
produced it — plus layer 1 on every management endpoint.

Every exemption is recorded with its reasoning in `db.rls.UNPROTECTED_TABLES`, and a test asserts
that no tenant-scoped table is absent from both lists. `verify_enforced` also flags an *unexpected*
policy on an excused table, which is how an orphan gets caught — see below.

## Two bugs this work produced, and what they cost

**An orphaned policy broke authentication entirely.** Moving `api_keys` to the exemption list left
its policy in place, because the migration's `downgrade` derived its table list from
`PROTECTED_TABLES` — code that had already changed. Every request returned 401. The downgrade now
discovers policies from the catalogue, and `verify_enforced` reports a policy on an excused table.

**The API could no longer start.** It created partitions at startup, which needs DDL privileges the
application role deliberately does not have — and attaching a partition additionally requires
owning the parent. That was a real design problem, not a permissions inconvenience: an application
that performs DDL at boot also races with itself across replicas. Partition creation moved to the
migration and to the worker's `maintain_partitions` job; startup now only *verifies* coverage and
logs loudly if a month is uncovered, because ingestion into an uncovered range fails outright.

```bash
make partitions   # create the coming months by hand
make app-role     # create the unprivileged role (needs APP_ROLE_PASSWORD)
```

## What is proven, and how

`apps/api/tests/test_rls.py` connects as a purpose-built `NOSUPERUSER NOBYPASSRLS` role and asserts:

- a tenant sees only its own rows
- **a query with no tenant predicate at all returns only the bound tenant's rows** — the layer-1 bug
  this exists to catch
- no tenant context returns nothing rather than erroring
- writing into another tenant is refused by `WITH CHECK`
- updating another tenant's row matches nothing
- the policy applies to partitions queried directly, not only through the parent
- the setting does not survive its transaction
- every tenant-scoped table is protected or has a recorded exemption

The first test in the file asserts that the *default* development role bypasses RLS. That is
deliberate: nobody should be able to read a green RLS suite as evidence of isolation without also
seeing that the default connection has none.

## Done in this phase

Each of these was listed as not done before, and the entry says what it now is — including what it
still is not, because a half-finished item claimed as finished is worse than an open one.

- **A cross-tenant sweep over every route.** `apps/api/tests/test_cross_tenant.py` parameterises 19
  by-id routes and 5 collections, and asserts against the OpenAPI schema that every remaining
  operation is explicitly excused with a reason. A new route cannot escape the sweep silently. Both
  tenants hold every scope, so a 403 for a missing permission cannot masquerade as isolation.
- **Graceful degradation when object storage fails.** Ingestion now accepts the batch without its
  large payloads, records a per-payload `RejectedItem`, and writes `{"_dropped":
  "storage_unavailable"}` in place of the payload. A trace without payloads still answers what the
  agent did, in what order, and what it cost — which is what policies and every operational metric
  are built on. A rejected batch answers nothing, and the SDK's buffer is bounded, so the data is
  gone for good.
- **DLQ handling and queue observability.** Jobs retry three times (`max_tries`), and the final
  failure is recorded in `worker_dead_letters` — on its own session, because the failing job's is
  being rolled back, and never raising, because it runs inside an exception handler. `GET
  /v1/ops/queues` reports job-queue depth (ready vs scheduled), dead letters by job with the age of
  the oldest unresolved one, and per-queue review depth. **No alerting.** Something still has to poll
  this; the endpoint is what makes that possible, not a substitute for it.
- **A fifteen-minute quickstart.** `docs/QUICKSTART.md`, verified end to end against a running
  system. Still markdown in a repository rather than a docs site.
- **One-command demo with seeded data.** `./scripts/demo.sh` — services, migrations, project, 60
  seeded traces of which 9 violate a policy, an online rule that has already run, a populated review
  queue, and the dashboard. It probes ports, and it says out loud that RLS is inert because the demo
  role is a superuser.
- **A load harness.** `tests/load/loadgen.py`, with committed results and an explicitly advisory
  verdict. Its first run found a real concurrency bug: environment auto-creation was
  check-then-insert, so a project's *first* burst lost 11 of 200 batches to a unique violation. Fixed
  with an upsert and covered by `TestConcurrentFirstBatch`.

## Not done

Stated plainly rather than implied by absence:

- **The CLI does not publish its run to the server.** `evalforge eval` computes, gates, and reports
  locally; `--local` defaults to true and remote execution is reserved. So a run does not appear
  under `/v1/experiments`, and the dashboard cannot show CI history. The server-side path itself
  works and is covered by `apps/api/tests/test_parity.py`; what is missing is the CLI sending
  results to it. `tests/e2e/test_acceptance.py` asserts the absence explicitly, so implementing
  publishing will surface as a failing assertion rather than an untested path.
- **E2E scenarios beyond the acceptance loop.** Annotate → promote → appears in the next run,
  offline spooling and replay, dataset immutability through the UI, and the calibration warning in
  CI are all described in `TESTING_STRATEGY.md` §5 and none are written.

- **Load numbers on the reference hardware.** The targets in `TESTING_STRATEGY.md` §8 are sized to
  4 vCPU / 8 GiB. The committed baseline was taken on a developer laptop with every service
  co-resident, so it is a regression baseline and not a pass. Also missing: the 10 M-span dataset the
  query targets are specified against, a 30-second sustained burst, worker throughput, experiment
  scheduling latency, and dashboard TTI. `tests/load/README.md` lists these against the targets they
  would answer.
- **Alerting.** `GET /v1/ops/queues` and `/readyz` report; nothing pages. There is no Prometheus
  endpoint and no alert rules.
- **A docs site.** These markdown files are the documentation.
- **Multi-region, backups, and restore drills.** Retention drops partitions correctly; nothing here
  has ever been restored from a backup, and an untested restore is not a backup.
- **Secrets management beyond environment variables.** No KMS, no envelope encryption for payloads at
  rest beyond what the object store provides.
