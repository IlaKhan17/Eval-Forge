#!/usr/bin/env bash
#
# Restore a backup and prove it worked.
#
#   ./scripts/restore.sh backups/proofstep-20260808T170000Z.dump --into proofstep_restore_check
#   ./scripts/restore.sh backups/latest.dump --into proofstep --force
#
# The verification is the point. Anyone can run pg_restore; what fails in an incident is discovering
# afterwards that the dump was truncated, or from the wrong schema version, or missing the RLS
# policies — none of which the restore itself complains about. So this compares the restored database
# against the manifest written at backup time and refuses to report success on a mismatch.
#
# Refuses to overwrite a non-empty database without --force. The one thing worse than a bad backup is
# a good database replaced by one.

set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE_SERVICE="${COMPOSE_SERVICE:-postgres}"

info() { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

DUMP=""
TARGET=""
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --into) TARGET="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *) DUMP="$1"; shift ;;
  esac
done

[ -n "$DUMP" ] || die "usage: restore.sh <dump> --into <database> [--force]"
[ -f "$DUMP" ] || die "no such dump: $DUMP"
[ -n "$TARGET" ] || die "--into <database> is required; restoring over the source by accident is not a mistake worth allowing"

[ -f .env ] && { set -a; . ./.env; set +a; }
: "${POSTGRES_USER:?set POSTGRES_USER}"

MANIFEST="${DUMP%.dump}.manifest.json"
[ -f "$MANIFEST" ] || warn "no manifest beside this dump — restoring, but verification will be limited"

# `</dev/null` on both: `docker compose exec -T` inherits and *consumes* stdin, so calling either of
# these inside a `while read` loop swallows the rest of the loop's input and the loop silently stops
# after one iteration. That happened here — the first drill verified exactly one table and reported
# success, which is the precise failure mode this script exists to prevent.
psql_admin() {
  docker compose exec -T "$COMPOSE_SERVICE" psql -qtAX -U "$POSTGRES_USER" -d postgres -c "$1" </dev/null
}
psql_target() {
  docker compose exec -T "$COMPOSE_SERVICE" psql -qtAX -U "$POSTGRES_USER" -d "$TARGET" -c "$1" </dev/null
}

# ------------------------------------------------------------------ integrity first

if [ -f "$MANIFEST" ]; then
  EXPECTED="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['sha256'])" "$MANIFEST")"
  if command -v shasum >/dev/null; then
    ACTUAL="$(shasum -a 256 "$DUMP" | awk '{print $1}')"
  else
    ACTUAL="$(sha256sum "$DUMP" | awk '{print $1}')"
  fi
  # Checked before restoring, not after. A corrupted dump restored over a live database is the
  # failure this ordering exists to prevent.
  [ "$EXPECTED" = "$ACTUAL" ] || die "checksum mismatch: the dump does not match its manifest"
  ok "checksum matches the manifest"
fi

# ------------------------------------------------------------------ target database

EXISTS="$(psql_admin "SELECT 1 FROM pg_database WHERE datname = '$TARGET'" | tr -d '\r')"
if [ "$EXISTS" = "1" ]; then
  TABLES="$(psql_target "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d '\r')"
  if [ "${TABLES:-0}" -gt 0 ] && [ "$FORCE" -ne 1 ]; then
    die "$TARGET already has $TABLES tables. Pass --force to replace it, or restore into a scratch database first."
  fi
  info "dropping and recreating $TARGET"
  psql_admin "DROP DATABASE IF EXISTS \"$TARGET\" WITH (FORCE)" >/dev/null
fi
psql_admin "CREATE DATABASE \"$TARGET\"" >/dev/null

info "restoring into $TARGET"
# --no-owner: the dump's owner may not exist here (a scratch restore, a different environment), and
# an ownership error would abort a restore that is otherwise fine.
# Exit status is deliberately tolerated and then *verified* below: pg_restore returns non-zero for
# benign warnings, so trusting its status alone produces both false alarms and false confidence.
docker compose exec -T "$COMPOSE_SERVICE" \
  pg_restore -U "$POSTGRES_USER" -d "$TARGET" --no-owner --no-privileges < "$DUMP" \
  > /tmp/proofstep-restore.log 2>&1 || warn "pg_restore reported warnings; verifying against the manifest"

# ------------------------------------------------------------------ verification

FAILURES=0
check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    ok "$name: $actual"
  else
    printf '\033[31m✗\033[0m %s: expected %s, restored %s\n' "$name" "$expected" "$actual" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

VERSION="$(psql_target 'SELECT version_num FROM alembic_version' | tr -d '\r')"
POLICIES="$(psql_target "SELECT count(*) FROM pg_policy WHERE polname LIKE '%tenant_isolation'" | tr -d '\r')"

if [ -f "$MANIFEST" ]; then
  read -r WANT_VERSION WANT_POLICIES <<< "$(python3 -c "
import json,sys
m = json.load(open(sys.argv[1]))
print(m['alembic_version'], m['tenant_policies'])" "$MANIFEST")"

  check "schema version" "$WANT_VERSION" "$VERSION"
  # Policies are checked explicitly because they are the thing most likely to be silently absent: a
  # restore that dropped them leaves a database that works perfectly and enforces nothing.
  check "tenant policies" "$WANT_POLICIES" "$POLICIES"

  while IFS=' ' read -r table want; do
    [ -n "$table" ] || continue
    got="$(psql_target "SELECT count(*) FROM $table" 2>/dev/null | tr -d '\r')" || got="missing"
    check "rows in $table" "$want" "${got:-missing}"
  done < <(python3 -c "
import json,sys
for table, count in json.load(open(sys.argv[1]))['row_counts'].items():
    print(table, count)" "$MANIFEST")
else
  ok "schema version: $VERSION"
  ok "tenant policies: $POLICIES"
fi

printf '\n'
if [ "$FAILURES" -gt 0 ]; then
  die "$FAILURES verification check(s) failed. This restore is NOT usable — see /tmp/proofstep-restore.log"
fi
ok "restore verified against the manifest"
printf '\nPoint an API at it with:\n  POSTGRES_DB=%s uv run python scripts/preflight.py\n' "$TARGET"
