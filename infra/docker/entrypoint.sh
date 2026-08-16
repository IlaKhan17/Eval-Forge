#!/usr/bin/env bash
#
# Which process this container runs.
#
# `migrate` is a separate command rather than something the API does on boot, and that separation is
# load-bearing. Migrations need a role that owns the schema; the application deliberately connects as
# one that cannot reshape it, because a role that can create a table can create one with no
# row-level-security policy. Running them on boot would also mean every replica racing the same DDL.

set -euo pipefail

case "${1:-api}" in
  api)
    # No --reload, no --workers by default: one process per container, replicas are the
    # orchestrator's job. `--proxy-headers` with a trusted-IP allow-list so client addresses in the
    # audit log are real rather than attacker-supplied.
    exec uvicorn proofstep_api.main:create_app \
      --factory \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --proxy-headers \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
    ;;
  worker)
    exec arq proofstep_api.worker.main.WorkerSettings
    ;;
  migrate)
    # Uses MIGRATION_DATABASE_URL when set; see infra/migrations/env.py.
    alembic upgrade head
    # Then the application role, in the same step and in this order. The grants cover the tables that
    # exist right now, so they have to be re-applied after the migration that added the newest ones —
    # otherwise a deploy succeeds and one endpoint starts returning permission errors.
    exec python scripts/provision_app_role.py
    ;;
  preflight)
    exec python scripts/preflight.py
    ;;
  *)
    exec "$@"
    ;;
esac
