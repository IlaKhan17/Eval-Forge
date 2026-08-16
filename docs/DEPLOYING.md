# Deploying Proofstep

Three ways to run it, in order of how much of it you want to operate yourself.

| | You run | Good for |
|---|---|---|
| [Docker Compose](#docker-compose) | one host, everything on it | trying it out, small teams, a single-tenant install |
| [Kubernetes](#kubernetes) | the app; managed Postgres, Redis, object storage | anything with a platform team |
| Proofstep Cloud | nothing | not wanting to run a database |

The images are `ghcr.io/ilakhan17/proofstep/api` and `ghcr.io/ilakhan17/proofstep/web`, built for
amd64 and arm64. Tags: a version (`0.1.0`), a minor series (`0.1`), or `edge` for the tip of main.
There is deliberately no `latest` — a tag whose meaning changes under you turns a pod reschedule
into an unplanned deploy and a rollback into archaeology.

Every image carries a build provenance attestation, so you can check where it came from before you
run it:

```bash
gh attestation verify --owner IlaKhan17 oci://ghcr.io/ilakhan17/proofstep/api:0.1.0
```

## Docker Compose

```bash
git clone https://github.com/IlaKhan17/proofstep && cd proofstep
./scripts/init_secrets.sh          # writes secrets/ and .env.prod
$EDITOR .env.prod                  # at minimum: PROOFSTEP_TAG, WEB_PORT, FORWARDED_ALLOW_IPS
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
./scripts/smoke_stack.sh http://127.0.0.1:3000
```

`smoke_stack.sh` signs up, mints a key, sends a trace, and reads it back. It is worth running: it
takes four requests to cross every part of the system, and each container bug this project has hit
so far was invisible to the test suite and obvious to that script.

**Secrets are files, not environment variables.** An environment variable is readable from
`/proc/<pid>/environ`, appears in `docker inspect`, and lands in crash reports. `init_secrets.sh`
writes `secrets/` at mode 600 and never overwrites — regenerating `jwt_secret` signs everyone out,
and regenerating a database password locks the application out of its own data until the role is
altered to match.

**The API is not published to the host.** The dashboard reaches it over the compose network. But
SDKs have to reach it from wherever your instrumented application runs, so a real deployment
publishes it somehow — behind your own TLS-terminating proxy, or with the overlay:

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.expose-api.yml \
  --env-file .env.prod up -d
```

That binds to loopback by default. `API_BIND=0.0.0.0` only when something in front of this host is
terminating TLS: API keys travel in the `Authorization` header, and plaintext HTTP is how they end
up in somebody else's logs.

### What runs where

```
web    (dashboard)     → api over the compose network, carrying the caller's session
api    (HTTP)          → postgres as the *application* role, redis, object storage
worker (background)    → the same, plus the owning role for its two DDL jobs
migrate (runs once)    → the owning role: alembic, then the application role's grants
```

`migrate` is a separate service that must complete before `api` and `worker` start. Migrations do
not run on boot: three replicas racing the same DDL is a deadlock waiting for a slow morning, and a
role that can create a table can create one with no row-level-security policy.

## Kubernetes

See [`infra/k8s/README.md`](../infra/k8s/README.md). Kustomize, no Helm — it is ten resources, and a
chart's templating language is a second thing to debug when the first thing is already YAML.

```bash
kubectl create namespace proofstep
kubectl -n proofstep create secret generic proofstep-secrets \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=database-url='postgresql+psycopg://proofstep_app:...@HOST:5432/proofstep' \
  --from-literal=migration-database-url='postgresql+psycopg://proofstep:...@HOST:5432/proofstep' \
  --from-literal=s3-secret-key='...'
kubectl apply -k infra/k8s
```

Postgres, Redis, and object storage are not included, on purpose. They are the stateful parts, they
are the parts whose loss is unrecoverable, and manifests that looked production-ready without being
so would be worse than their absence.

## Before you call it production

The full list is [`docs/HARDENING.md`](HARDENING.md). The four that actually bite:

1. **The application must not own its tables.** Both deployment paths handle this — `migrate` runs
   as the owner and provisions a separate application role — but verify it rather than assume:

   ```bash
   curl -s https://api.example.com/readyz
   {"status": "ready", "checks": {"row_level_security": "enforced (26/26 tables)"}}
   ```

   If that says `not_enforced`, every row-level-security policy in the database exists and does
   nothing, and nothing else in the system's behaviour will tell you.

2. **TLS, in front of everything.** Neither the compose stack nor the manifests terminate it.

3. **`FORWARDED_ALLOW_IPS`, narrowed to your proxy.** Left wide, `X-Forwarded-For` is
   caller-controlled and every client address in the audit log is whatever the caller claimed.

4. **Backups you have restored from.** `scripts/backup.sh` and `scripts/restore.sh` exist;
   a backup nobody has restored is a hypothesis.
