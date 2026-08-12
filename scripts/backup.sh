#!/usr/bin/env bash
#
# Take a verifiable backup of the EvalForge database.
#
#   ./scripts/backup.sh                      # writes ./backups/evalforge-<ts>.dump
#   BACKUP_DIR=/mnt/backups ./scripts/backup.sh
#
# What this is and is not: a **logical** backup — one consistent snapshot, taken now. It is the right
# tool for a pre-deploy safety net, a migration rehearsal, and moving data between environments. It
# is *not* point-in-time recovery: everything written between two runs of this script is gone if the
# primary is lost. PITR needs continuous WAL archiving on the server, which is a Postgres
# configuration rather than a script; docs/OPERATIONS.md says what to set and why.
#
# Every backup is written with a manifest recording the schema version and the row counts of the
# tables that matter. Restoring is not the hard part — knowing whether the restore *worked* is, and
# a dump nobody can verify is a dump nobody trusts under pressure.
#
# Payloads in object storage are NOT included. They live in S3/MinIO with their own lifecycle, and a
# dump that silently omitted them while looking complete would be worse than one that says so.

set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-postgres}"

info() { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[ -f .env ] && { set -a; . ./.env; set +a; }
: "${POSTGRES_USER:?set POSTGRES_USER}"
: "${POSTGRES_DB:?set POSTGRES_DB}"

# Timestamped and sortable. A backup that overwrites the previous one is a single point of failure
# wearing a backup's clothes.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASE="$BACKUP_DIR/evalforge-$STAMP"
mkdir -p "$BACKUP_DIR"

# Run psql/pg_dump inside the container by default, so the host does not need a matching client
# version — a pg_dump older than the server refuses outright, which is a confusing first failure.
# `</dev/null`: `docker compose exec -T` consumes stdin, which silently truncates any loop that
# calls this while reading from stdin. See the same note in restore.sh.
psql_run() {
  docker compose exec -T "$COMPOSE_SERVICE" psql -qtAX -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1" </dev/null
}

docker compose ps "$COMPOSE_SERVICE" >/dev/null 2>&1 || die "compose service '$COMPOSE_SERVICE' is not running"

info "dumping $POSTGRES_DB"
# Custom format: parallel restore, selective restore, and compression, none of which plain SQL gives.
# --clean --if-exists so the dump can be replayed over an existing database without a manual drop.
docker compose exec -T "$COMPOSE_SERVICE" \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --clean --if-exists \
  > "$BASE.dump" || die "pg_dump failed"

SIZE="$(wc -c < "$BASE.dump" | tr -d ' ')"
[ "$SIZE" -gt 1000 ] || die "the dump is $SIZE bytes, which cannot be right"

info "recording a manifest"
VERSION="$(psql_run 'SELECT version_num FROM alembic_version' | tr -d '\r')"
POLICIES="$(psql_run "SELECT count(*) FROM pg_policy WHERE polname LIKE '%tenant_isolation'" | tr -d '\r')"

# Counts for the tables a restore has to be verified against. Deliberately a short list: these are
# the ones whose absence would be catastrophic and whose presence proves the dump was not truncated.
counts_json() {
  local first=1
  printf '{'
  for table in organizations projects api_keys traces spans experiments experiment_runs \
               evaluation_results online_evaluations review_assignments; do
    local n
    n="$(psql_run "SELECT count(*) FROM $table" 2>/dev/null | tr -d '\r')" || n=0
    [ -n "$n" ] || n=0
    [ $first -eq 1 ] || printf ','
    printf '"%s":%s' "$table" "$n"
    first=0
  done
  printf '}'
}

# sha256 over the dump, so a silently corrupted file is caught before someone relies on it.
if command -v shasum >/dev/null; then
  DIGEST="$(shasum -a 256 "$BASE.dump" | awk '{print $1}')"
else
  DIGEST="$(sha256sum "$BASE.dump" | awk '{print $1}')"
fi

cat > "$BASE.manifest.json" <<JSON
{
  "created_at": "$STAMP",
  "database": "$POSTGRES_DB",
  "dump": "$(basename "$BASE.dump")",
  "bytes": $SIZE,
  "sha256": "$DIGEST",
  "alembic_version": "$VERSION",
  "tenant_policies": $POLICIES,
  "row_counts": $(counts_json),
  "excludes": "payloads in object storage — see docs/OPERATIONS.md"
}
JSON

ok "$BASE.dump ($(( SIZE / 1024 )) KiB)"
ok "$BASE.manifest.json — schema $VERSION, $POLICIES tenant policies"
printf '\nRestore with:\n  ./scripts/restore.sh %s --into evalforge_restore_check\n' "$BASE.dump"
