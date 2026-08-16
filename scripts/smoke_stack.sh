#!/usr/bin/env bash
#
# Exercise a running deployment end to end, through its front door.
#
#   ./scripts/smoke_stack.sh http://127.0.0.1:3000 http://127.0.0.1:8000
#
# Two URLs, because the product has two front doors and they authenticate differently: a browser
# reaches the dashboard and rides a session cookie through its proxy, while an SDK reaches the API
# with a key. Testing only one of them leaves half the product unproven. The API argument is
# optional; without it the ingest half is skipped and says so.
#
# Sign up, mint a key, send a trace, read it back. Four requests, and between them they touch every
# piece of the system: the dashboard's proxy and its session cookies, the API's auth and its
# unprivileged database role, the ingest path, object storage, and every row-level-security policy
# on the way through.
#
# This exists because the container bugs found so far were all invisible to the test suite. A
# dependency that only the development environment installed. A settings guard that demanded a
# credential the process should not hold. An insert with no tenant set, which a superuser connection
# accepts and the production role refuses — so signup, the first request any new user makes, worked
# in development and returned a 500 in production. None of those are reachable from pytest, and all
# of them are reachable from four curl calls against the real thing.
#
# Safe to run against a fresh deployment. It creates one account, and says which one.

set -euo pipefail

BASE="${1:-http://127.0.0.1:3000}"
# Empty when not given. The base compose file does not publish the API — see
# docker-compose.expose-api.yml — so on a default deployment there is nothing to point at.
API="${2:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

JAR="$WORK/cookies"
EMAIL="smoke-$(date +%s)-$RANDOM@example.com"
# Long enough for the production password policy, and useless: this account exists to prove the
# system works, on a domain reserved by RFC 2606 so the address can never belong to anyone.
# `example.com` and not `.invalid`: the API validates addresses properly, and a special-use TLD is
# one of the things a proper validator rejects.
PASSWORD="smoke-test-account-not-a-real-password"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }

# The dashboard requires this header on every write. It is half of the CSRF defence — a cross-origin
# page cannot set a custom header without a preflight the API refuses — so a smoke test that omitted
# it would be testing a path no browser takes.
# The value is checked, not just the presence: it must be exactly "1".
HDRS=(-H 'content-type: application/json' -H 'x-proofstep-request: 1')

json() { python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2"; }

request() {
  local method="$1" path="$2" body="${3:-}" out="$4"
  local args=(-s -o "$out" -w '%{http_code}' -b "$JAR" -c "$JAR" -X "$method" "${HDRS[@]}" "$BASE$path")
  [ -n "$body" ] && args+=(-d "$body")
  curl "${args[@]}"
}

say "1. sign up  ($EMAIL)"
code=$(request POST /api/auth/signup \
  "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\",\"organization_name\":\"Smoke Test\"}" \
  "$WORK/signup.json")
[ "$code" = "200" ] || fail "signup returned $code: $(cat "$WORK/signup.json")"
PROJECT="$(json "$WORK/signup.json" project_id)"
[ -n "$PROJECT" ] || fail "signup returned no project — an account with nowhere to work is not a signup"
ok "account created, project $PROJECT"

say "2. mint an API key"
code=$(request POST "/api/ps/v1/projects/$PROJECT/api-keys" \
  '{"name":"smoke","scopes":["ingest","read"]}' "$WORK/key.json")
[ "$code" = "201" ] || fail "key creation returned $code: $(cat "$WORK/key.json")"
KEY="$(json "$WORK/key.json" token)"
[ -n "$KEY" ] || fail "no token in the response — the secret is returned exactly once, at creation"
ok "key ${KEY:0:16}… created"

if [ -z "$API" ]; then
  say "3. skipped: no API URL given"
  echo "  Pass one to exercise ingestion, e.g. $0 $BASE http://127.0.0.1:8000"
  echo "  (docker-compose.expose-api.yml publishes it; the base file deliberately does not.)"
  say "the control plane works: signup, then a key. Ingestion was not tested."
  exit 0
fi

say "3. send a trace, and read it back"
# Straight to the API with the key, because that is what an SDK does. Not through the dashboard
# proxy: its allow-list does not carry ingestion, and it attaches the session rather than the key.
# Two front doors, two credentials, and only one of them is a browser.
python3 - "$API" "$KEY" <<'PY'
import datetime, json, sys, urllib.error, urllib.request, uuid

api, key = sys.argv[1], sys.argv[2]
now = datetime.datetime.now(datetime.UTC).isoformat()
trace_id, span_id = uuid.uuid4().hex, uuid.uuid4().hex[:16]

batch = {
    "resource": {"service.name": "smoke"},
    "traces": [
        {
            "trace_id": trace_id,
            "name": "smoke-test",
            "environment": "production",
            "started_at": now,
            "ended_at": now,
        }
    ],
    "spans": [
        {
            "trace_id": trace_id,
            "span_id": span_id,
            "name": "llm.call",
            "span_type": "llm",
            "started_at": now,
            "ended_at": now,
            "input": {"prompt": "does this deployment work"},
            "output": {"text": "we are about to find out"},
        }
    ],
}


def call(path, payload=None):
    request = urllib.request.Request(
        f"{api}{path}",
        data=json.dumps(payload).encode() if payload else None,
        headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:300]


status, body = call("/v1/ingest/traces", batch)
if status != 202:
    sys.exit(f"ingest returned {status}: {body}")
# Counted, not assumed. A batch whose spans were silently dropped still returns 202 — the whole
# point of the ingest contract is that a partial acceptance says so.
if body.get("accepted_spans") != 1:
    sys.exit(f"ingest accepted the trace but not its span: {body}")
print(f"\033[32m✓\033[0m ingest accepted 1 trace, 1 span")

status, body = call(f"/v1/traces/{trace_id}")
if status != 200:
    sys.exit(f"reading the trace back returned {status}: {body}")
if body.get("span_count") != 1:
    sys.exit(f"the trace came back with span_count={body.get('span_count')}, expected 1")
print(f"\033[32m✓\033[0m trace {trace_id[:12]}… read back with its span")
PY

say "the deployment works: signup → key → ingest → read"
