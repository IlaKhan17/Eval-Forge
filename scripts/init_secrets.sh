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

cat <<'NOTE'

Next:
  1. cp .env.prod.example .env.prod
  2. Put the *same* owner password into .env.prod as OWNER_DB_PASSWORD — Postgres reads it from
     the secret file, and the migration URL needs it inline because a connection string cannot
     reference a file. That duplication is the one place a secret appears twice; it is why
     .env.prod belongs in the same place as the secrets directory and out of git.
  3. docker compose -f docker-compose.prod.yml --env-file .env.prod up -d

Back these up somewhere you can reach when the host is gone. Losing jwt_secret signs everyone out;
losing app_db_password is recoverable with ALTER ROLE, but only if you can still reach the database.
NOTE
