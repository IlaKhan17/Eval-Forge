# Operations

Running EvalForge somewhere real. `HARDENING.md` explains *why* the isolation model is what it is;
this is the procedure, plus the two things that only exist in an operator's world — backups and
alerts.

Everything here has been executed against a running system. Where something has **not** been
exercised, it says so rather than reading like a completed step.

---

## 1. Roles: the application must not own its tables

The single most important deployment step, and the easiest to skip because skipping it changes
nothing observable. Row-level security applies to neither a superuser nor a table's owner (unless
every policy carries `FORCE`, which a future migration can silently drop). Connect as a role that is
neither, and tenant isolation has three layers instead of one.

```bash
# once, as a superuser
psql -v role_password="$(openssl rand -hex 24)" -f scripts/create_app_role.sql
```

Then two roles in the environment:

```bash
# the application: no DDL, no ownership, subject to every policy
POSTGRES_USER=evalforge_app
POSTGRES_PASSWORD_FILE=/run/secrets/db-password

# migrations and the worker's DDL jobs: owns the schema
MIGRATION_DATABASE_URL=postgresql+psycopg://evalforge:...@db:5432/evalforge
```

`MIGRATION_DATABASE_URL` is used by Alembic and by the worker's two DDL jobs (partition maintenance
and retention). Everything else uses the application role. **In production the API refuses to start**
when its role bypasses RLS; `ALLOW_RLS_BYPASS=1` is the deliberate escape hatch, and it warns on
every boot so it cannot be set once and forgotten.

Verify:

```bash
uv run python scripts/preflight.py     # exits non-zero on a blocking problem
curl -s localhost:8000/readyz | jq .checks.row_level_security
# "enforced (26/26 tables)"
```

**Verified.** The full stack — ingest, online evaluation, review queues, CLI publishing, and the
worker — has been run end to end as `evalforge_app` with 26/26 tables enforced.

---

## 2. Secrets and keys

### Secrets from files, not environment variables

Every sensitive setting accepts a `<NAME>_FILE` variant pointing at a file: `JWT_SECRET_FILE`,
`POSTGRES_PASSWORD_FILE`, `S3_SECRET_KEY_FILE`, `DATABASE_URL_FILE`, `MIGRATION_DATABASE_URL_FILE`.
This is the convention Docker secrets and Kubernetes secret volumes already speak, and it matters
because an environment variable is readable from `/proc/<pid>/environ`, appears in `docker inspect`,
and lands in crash reports. A file has an owner and a mode.

An empty file is treated as absent — "the orchestrator has not populated this yet" produces a
clearer failure than "your signing key is the empty string".

### API keys

`scripts/manage_keys.py` works in production (`bootstrap_dev.py` deliberately does not):

```bash
uv run python scripts/manage_keys.py list   --project acme
uv run python scripts/manage_keys.py create --project acme --name ci --scopes ingest read --expires-days 90
uv run python scripts/manage_keys.py rotate --prefix ef_prod_ab12cd34 --grace-hours 24
uv run python scripts/manage_keys.py revoke --prefix ef_prod_ab12cd34 --reason "rotated"
```

Rotation is **overlap, not replacement**: the new key is minted and the old one is given an expiry
rather than being revoked on the spot, because a rotation that breaks every running job the moment
it happens is a rotation nobody performs. Update the consumers, then revoke.

Revocation takes effect within `API_KEY_CACHE_TTL_S` (30s default) — worth knowing before the
half-minute where a revoked key still works becomes alarming.

Every action is written to `audit_logs`. `preflight.py` fails when a development-issued key
(`ef_dev_*`, `ef_test_*`) is still live in production, which is the normal residue of a database
promoted from a development install.

**Verified.** Create → 200, rotate → both keys valid during the grace window, revoke → 401 after the
cache expires.

### TLS and proxies

Not handled by the application, deliberately: terminate TLS at your ingress. Two things to configure
there, because they are wrong by default —

- Run uvicorn with `--proxy-headers --forwarded-allow-ips=<your proxy>` so client IPs in audit logs
  are real. Without the allow-list, `X-Forwarded-For` is attacker-controlled.
- Add HSTS at the proxy. The app sets `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  and `Permissions-Policy`, but a service that cannot see its own scheme should not be asserting
  transport policy.

**Not exercised.** Nothing here has run behind a real proxy.

---

## 3. Backups

```bash
./scripts/backup.sh                                     # → backups/evalforge-<ts>.dump + manifest
./scripts/restore.sh backups/<file>.dump --into evalforge_restore_check
```

The backup writes a manifest beside the dump: sha256, schema version, tenant-policy count, and row
counts for ten tables. The restore verifies against it and **refuses to report success on a
mismatch**. That is the whole point — anyone can run `pg_restore`; what fails in an incident is
discovering afterwards that the dump was truncated or came from a different schema version.

`restore.sh` refuses to overwrite a non-empty database without `--force`.

**Verified.** A full drill has been run: backup → restore into a scratch database → all ten row
counts, the schema version, and all 41 policies match → `preflight.py` passes against the restored
database.

### What this does not cover

- **Point-in-time recovery.** These are logical snapshots; everything written between two runs is
  lost if the primary is. PITR needs continuous WAL archiving, which is Postgres configuration
  rather than a script: set `archive_mode = on`, an `archive_command` that ships to durable storage,
  and `wal_level = replica`. **Not configured here, and not exercised.**
- **Payloads in object storage.** Large span payloads live in S3/MinIO with their own lifecycle. The
  manifest says so rather than letting a complete-looking dump imply otherwise. Use bucket
  versioning and replication.
- **A restore rehearsal on production-sized data.** The drill above ran against ~34k spans. Restore
  time is not linear in a way you want to discover during an incident.

### Schedule

Daily is the usual answer; the right one depends on how much evaluation history you can afford to
lose. Run `backup.sh` from cron or a Kubernetes CronJob, ship the output off-host, and **restore
from it periodically** — an untested backup is not a backup, and the verification in `restore.sh`
exists so that test is one command.

---

## 4. Monitoring and alerts

`GET /metrics` exposes Prometheus text. It requires a bearer token with the `read` scope —
Prometheus supports `authorization.credentials_file`, so this is one line of scrape config, and it
avoids adding an unauthenticated surface to a service whose threat model is about who may read what.

`infra/alerts/evalforge.rules.yml` has nine rules, each for a failure that is otherwise **silent**:

| Alert | Fires when | Why it is not obvious |
|---|---|---|
| `EvalForgeWorkerStopped` | no heartbeat for 5 min | the API stays healthy; only new data stops appearing |
| `EvalForgeNoWorkerRegistered` | no worker ever beat | a deployment with no worker looks perfect from outside |
| `EvalForgeQueueUnreachable` | Redis unreadable | depth 0 and "cannot see it" look identical in a number |
| `EvalForgeQueueBacklog` | >100 ready jobs for 15 min | deferred cron jobs are excluded, or it fires constantly |
| `EvalForgeDeadLetters` | any unresolved failure | arq drops a job after its retries, silently |
| `EvalForgeDeadLettersUnattended` | oldest >3 days | nobody is reading the first alert |
| `EvalForgeReviewQueueStale` | oldest pending >7 days | a queue nobody reads still looks like a control |
| `EvalForgeTenantIsolationNotEnforced` | `rls_enforced == 0` | behaviour is identical either way |
| `EvalForgeDown` | scrape fails | |

Worker liveness comes from a heartbeat row written every minute by a dedicated cron job **and** after
every job. A row rather than a Redis key with a TTL: when Redis is what broke, a TTL-based signal
disappears exactly when it is needed, and "the worker is down" becomes indistinguishable from "I
cannot tell".

**Verified.** Metrics scraped from a live API; heartbeats appear within a minute of the worker
starting and the gauge reports their age.

**Not exercised.** No alert has actually fired into a pager — the rules are written and parse, but
routing, inhibition, and on-call escalation are yours.

---

## Deploy sequence

```bash
uv run alembic upgrade head          # as the migration role
uv run python scripts/preflight.py   # blocks the deploy on a failed check
# start the API, start the worker, then route traffic
```

The worker is not optional. Without it there is no online evaluation, no review-queue escalation, no
lease recovery, and no retention — and, as above, nothing about the API's behaviour says so.

## Still open

Listed here rather than left to be discovered — `HARDENING.md §Not done` is the fuller list.

- **Load verification on reference hardware** (4 vCPU / 8 GiB), including query latency against a
  10M-span dataset. `tests/load/README.md` says what the committed numbers do and do not mean.
- **Rate limiting on ingest.** Settings exist; enforcement does not.
- **Deployment manifests.** No Kubernetes or systemd units ship here.
- **An org-level spend ceiling** for judge costs. Per-suite `max_cost` exists; nothing caps a month.
