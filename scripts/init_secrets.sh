#!/usr/bin/env bash
#
# Generate the secret files the production stack mounts.
#
#   ./scripts/init_secrets.sh
#
# Files rather than environment variables, because an env var is readable from /proc/<pid>/environ,
# appears in `docker inspect`, and lands in crash reports. A file has an owner and a mode.
#
# Refuses to overwrite. Regenerating the JWT secret signs every existing session out; regenerating a
# database password locks the application out of its own data until the role is altered to match.
# Both are recoverable and neither is something to do by accident.

set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p secrets
chmod 700 secrets

generate() {
  local name="$1" path="secrets/$1"
  if [ -f "$path" ]; then
    printf '  = %-20s already exists, left alone\n' "$name"
    return
  fi
  openssl rand -hex 32 > "$path"
  chmod 600 "$path"
  printf '  + %-20s created\n' "$name"
}

echo "secrets/"
generate owner_db_password
generate app_db_password
generate jwt_secret
generate s3_secret_key

# .env.prod, with the one secret that cannot live in a file already filled in. Postgres reads the
# owner password from its secret file, but the migration URL needs it inline — a connection string
# cannot reference a file — so it has to appear in both places. Copying it by hand is a step that is
# easy to get subtly wrong (a trailing newline, the wrong file) and whose failure mode is an
# authentication error three services deep.
#
# Same rule as the secrets: never overwrite. This file accumulates real configuration.
if [ -f .env.prod ]; then
  echo
  echo "  = .env.prod              already exists, left alone"
else
  owner_password="$(cat secrets/owner_db_password)"
  sed "s|^OWNER_DB_PASSWORD=.*|OWNER_DB_PASSWORD=${owner_password}|" .env.prod.example > .env.prod
  chmod 600 .env.prod
  echo
  echo "  + .env.prod              created from .env.prod.example, owner password filled in"
fi

cat <<'NOTE'

Next:
  1. Review .env.prod — at minimum PROOFSTEP_TAG, WEB_PORT, and FORWARDED_ALLOW_IPS.
  2. docker compose -f docker-compose.prod.yml --env-file .env.prod up -d

Back these up somewhere you can reach when the host is gone. Losing jwt_secret signs everyone out;
losing app_db_password is recoverable with ALTER ROLE, but only if you can still reach the database.
NOTE
