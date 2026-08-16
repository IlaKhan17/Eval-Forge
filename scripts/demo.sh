#!/usr/bin/env bash
#
# One command to a working Proofstep: services, schema, a project, seeded traces, and the dashboard.
#
#   ./scripts/demo.sh
#
# Idempotent — safe to re-run. It reuses the project and adds another round of traces.
#
# Deliberately a shell script rather than a compose profile. The API and worker run on the host in
# this repository (compose carries only Postgres, Redis, and MinIO), and a demo that quietly
# introduced a second way to run the application would double the surface that has to stay working.

set -euo pipefail
cd "$(dirname "$0")/.."

info()  { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# Ports are probed, not assumed, for the same reason scripts/gen-dev-env.sh probes the service ports:
# 8000 and 3000 are the two most contended ports on any developer's machine, and "address already in
# use" from someone else's project is a needless first-run failure. Override either explicitly to pin.
free_port() {
  local preferred=$1 fallback=$2
  if lsof -nP -iTCP:"$preferred" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "$fallback"
  else
    echo "$preferred"
  fi
}

API_PORT="${API_PORT:-$(free_port 8000 8010)}"
WEB_PORT="${WEB_PORT:-$(free_port 3000 3010)}"
PIDS=()

cleanup() {
  # Only the processes this script started. Leaving a stray uvicorn behind is how the next run fails
  # with an unhelpful "address in use".
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------- prerequisites

command -v uv >/dev/null || die "uv is not installed — see https://docs.astral.sh/uv/"
command -v docker >/dev/null || die "docker is not installed"
docker info >/dev/null 2>&1 || die "docker is not running"

if [ ! -f .env ]; then
  info "generating .env (ports are probed, so this will not collide with your other projects)"
  ./scripts/gen-dev-env.sh
fi
set -a; . ./.env; set +a

# ---------------------------------------------------------------- services

info "starting postgres, redis, and minio"
# Both streams: compose writes its pull and create progress to stderr, so >/dev/null alone leaves
# several hundred lines of layer download bars in the middle of the demo's output.
docker compose up -d postgres redis minio >/dev/null 2>&1

info "waiting for postgres"
for _ in $(seq 1 60); do
  if docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" >/dev/null 2>&1; then break; fi
  sleep 1
done
docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" >/dev/null 2>&1 \
  || die "postgres did not become ready"
ok "services up"

# ---------------------------------------------------------------- schema and tenant

info "applying migrations"
uv run alembic upgrade head >/dev/null
uv run python -c "$(cat <<'PY'
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from proofstep_api.db.partitions import ensure_partitions
from proofstep_api.settings import get_settings

async def main():
    engine = create_async_engine(get_settings().sqlalchemy_url)
    async with engine.begin() as conn:
        await ensure_partitions(conn)
    await engine.dispose()

asyncio.run(main())
PY
)" >/dev/null
ok "schema ready"

info "creating the demo project and an API key"
KEY="$(uv run python scripts/bootstrap_dev.py --org demo --project demo 2>/dev/null \
  | grep -o 'ps_dev_[A-Za-z0-9_-]*' | head -1)"
[ -n "$KEY" ] || die "could not create an API key — see the output of scripts/bootstrap_dev.py"
export PROOFSTEP_API_KEY="$KEY"

# Written here rather than with bootstrap_dev.py's --write-web-env, which hardcodes :8000. The
# dashboard has to point at the port we actually got.
#
# No NEXT_PUBLIC_ prefix: that prefix is what would inline the key into the browser bundle. The
# dashboard reads it server-side and proxies. See apps/web/src/lib/api.ts.
cat > apps/web/.env.local <<ENVEOF
# Written by scripts/demo.sh — local only, not committed.
PROOFSTEP_API_URL=http://127.0.0.1:$API_PORT
PROOFSTEP_API_KEY=$KEY
ENVEOF
ok "project ready"

# ---------------------------------------------------------------- api

info "starting the API on :$API_PORT"
uv run uvicorn proofstep_api.main:create_app --factory --host 127.0.0.1 --port "$API_PORT" \
  >/tmp/proofstep-demo-api.log 2>&1 &
PIDS+=("$!")

for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$API_PORT/readyz" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
curl -sf "http://127.0.0.1:$API_PORT/readyz" >/dev/null \
  || die "the API did not become ready — see /tmp/proofstep-demo-api.log"
ok "API ready"

# The demo runs as the default superuser role, so RLS is installed but inert. Said out loud rather
# than left for someone to discover: a demo that implied production-grade isolation would be
# misleading, and the fix is one documented command.
if curl -s "http://127.0.0.1:$API_PORT/readyz" | grep -q not_enforced; then
  warn "row-level security is not in effect (the demo role is a superuser)."
  warn "For a real deployment see docs/HARDENING.md — one script and two env vars."
fi

# ---------------------------------------------------------------- seed

info "seeding traces, an online rule, and a review queue"
PROOFSTEP_API_KEY="$KEY" PROOFSTEP_ENDPOINT="http://127.0.0.1:$API_PORT" \
  uv run python scripts/seed_demo.py

# ---------------------------------------------------------------- dashboard

if [ -d apps/web/node_modules ]; then
  info "starting the dashboard on :$WEB_PORT"
  (cd apps/web && pnpm dev --port "$WEB_PORT" >/tmp/proofstep-demo-web.log 2>&1) &
  PIDS+=("$!")
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$WEB_PORT/traces" >/dev/null 2>&1; then break; fi
    sleep 0.5
  done
  ok "dashboard ready"
else
  warn "dashboard dependencies are not installed; run 'make web-install' then './scripts/demo.sh'"
fi

printf '\n'
ok "Proofstep is running"
printf '\n'
printf '  dashboard   http://127.0.0.1:%s/traces\n' "$WEB_PORT"
printf '  API docs    http://127.0.0.1:%s/docs\n' "$API_PORT"
printf '  API key     %s\n' "$KEY"
printf '\n'
printf '  Try:\n'
printf '    uv run proofstep eval evals/suites/davis-agent-policy.yaml\n'
printf '    DAVIS_BREAK_POLICY=1 uv run proofstep eval evals/suites/davis-agent-policy.yaml\n'
printf '\n'
printf 'Ctrl-C to stop.\n'

# Wait on the API rather than sleeping, so Ctrl-C is immediate and a crash surfaces.
wait "${PIDS[0]}"
