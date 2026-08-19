/**
 * What the proxy is allowed to forward.
 *
 * The proxy now carries the *caller's own session* rather than a shared server-side key, which
 * changes what this list is for. It is no longer preventing a confused deputy — a user acting as
 * themselves is not one — it is keeping the dashboard's reachable surface deliberately small.
 *
 * That still matters. An allow-list means a new API endpoint is not reachable from a browser until
 * somebody decides it should be, and the endpoints with the widest blast radius stay off it:
 * `POST /v1/experiments/{id}/promote-baseline` changes what every future gate compares against, and
 * nothing in the UI needs it yet.
 *
 * Writes are a separate list from reads, so "the dashboard can read X" never silently implies "the
 * dashboard can write X".
 *
 * Pure and separately tested, because a mistake here is a security bug and route handlers are
 * awkward to test.
 */

const ALLOWED_GET_PATTERNS: readonly RegExp[] = [
  /^\/readyz$/,
  /^\/v1\/traces$/,
  // Trace ids are opaque strings from the SDK (hex, or whatever an OTLP client sends),
  // so the character class is permissive but must not admit a path separator — that is
  // what would turn one endpoint into all of them.
  /^\/v1\/traces\/[A-Za-z0-9._:-]{1,256}$/,
  /^\/v1\/datasets$/,
  // Experiment history. Every one is a read: the list, one experiment's runs, and a run's metrics.
  // Notably *not* here: POST /v1/experiments/{id}/promote-baseline, which changes what future gates
  // compare against — a quiet, high-impact write that has no business being reachable from a
  // read-only viewer. The method check above already refuses it; keeping it out of this list too is
  // the belt to that braces.
  /^\/v1\/experiments$/,
  /^\/v1\/experiments\/[0-9a-f-]{36}\/runs$/,
  /^\/v1\/experiment-runs\/[0-9a-f-]{36}\/metrics$/,
  // The account surface: who am I, which workspaces, who else is here, which keys exist.
  /^\/v1\/auth\/me$/,
  /^\/v1\/orgs$/,
  /^\/v1\/orgs\/[0-9a-f-]{36}\/members$/,
  /^\/v1\/orgs\/[0-9a-f-]{36}\/invites$/,
  /^\/v1\/orgs\/[0-9a-f-]{36}\/projects$/,
  /^\/v1\/projects\/[0-9a-f-]{36}\/api-keys$/,
  /^\/v1\/ops\/queues$/,
  /^\/v1\/ops\/budget$/,
  // Resolving an invitation link. Unauthenticated by necessity — the person following it usually
  // has no account yet — and safe to expose: it answers only for a token the caller already holds,
  // and the API rate-limits it so it cannot be ground against.
  /^\/v1\/invites\/preview$/,
]

export interface PolicyDecision {
  allowed: boolean
  /** Reason, for the 403 body and the server log. Only set when denied. */
  reason?: string
  /**
   * Whether this path may be forwarded with no session attached.
   *
   * False for everything except the handful below, and the default matters: the proxy refuses an
   * unauthenticated request outright, which is right for every endpoint that reads tenant data and
   * wrong for the two or three that exist precisely for people who have no account yet.
   */
  anonymous?: boolean
}

/**
 * Paths the proxy forwards without a session.
 *
 * Kept to what genuinely cannot require one. An invitation link is followed by someone who, in the
 * common case, has never used this product — demanding a session first would mean the only people
 * who can read an invitation are the ones who do not need it.
 *
 * Nothing here reaches tenant data. `/v1/invites/preview` resolves a token the caller already holds
 * and returns what the invitation email they are reading already told them, and the API rate-limits
 * it so the token cannot be guessed at.
 */
const ANONYMOUS_PATTERNS: readonly RegExp[] = [/^\/v1\/invites\/preview$/]

/**
 * Writes the dashboard may make, as `METHOD /path` patterns.
 *
 * Every one is an account action a person performs about their own workspace: invite a colleague,
 * change a role, mint or revoke a key, set the spend ceiling. Nothing here touches evaluation data —
 * datasets, gates, and baselines are versioned artefacts that belong in a repository and a review,
 * not behind a button.
 */
const ALLOWED_WRITE_PATTERNS: readonly { method: string; pattern: RegExp }[] = [
  { method: "POST", pattern: /^\/v1\/orgs$/ },
  { method: "POST", pattern: /^\/v1\/orgs\/[0-9a-f-]{36}\/projects$/ },
  { method: "POST", pattern: /^\/v1\/orgs\/[0-9a-f-]{36}\/invites$/ },
  { method: "POST", pattern: /^\/v1\/invites\/accept$/ },
  { method: "PATCH", pattern: /^\/v1\/orgs\/[0-9a-f-]{36}\/members\/[0-9a-f-]{36}$/ },
  { method: "DELETE", pattern: /^\/v1\/orgs\/[0-9a-f-]{36}\/members\/[0-9a-f-]{36}$/ },
  { method: "POST", pattern: /^\/v1\/projects\/[0-9a-f-]{36}\/api-keys$/ },
  { method: "DELETE", pattern: /^\/v1\/projects\/[0-9a-f-]{36}\/api-keys\/[0-9a-f-]{36}$/ },
  { method: "PUT", pattern: /^\/v1\/ops\/budget$/ },
]

export function checkProxyRequest(method: string, path: string): PolicyDecision {
  if (!path.startsWith("/")) {
    return { allowed: false, reason: "Path must be absolute." }
  }
  // `..` cannot survive URL normalization in a browser, but this proxy can also be
  // called by anything else, and a traversal that escapes the allow-list would defeat
  // the entire mechanism.
  if (path.includes("..") || path.includes("//") || path.includes("\\")) {
    return { allowed: false, reason: "Path is not in a normalized form." }
  }

  const anonymous = ANONYMOUS_PATTERNS.some((pattern) => pattern.test(path))

  if (method === "GET" || method === "HEAD") {
    if (!ALLOWED_GET_PATTERNS.some((pattern) => pattern.test(path))) {
      return { allowed: false, reason: `The dashboard proxy does not expose ${path}.` }
    }
    return { allowed: true, anonymous }
  }

  const writable = ALLOWED_WRITE_PATTERNS.some(
    (entry) => entry.method === method && entry.pattern.test(path),
  )
  if (!writable) {
    return {
      allowed: false,
      reason: `The dashboard proxy does not allow ${method} ${path}. Evaluation artefacts — datasets, gates, baselines — are changed through the CLI so the change lands in a repository and a review.`,
    }
  }
  return { allowed: true }
}

/**
 * Query parameters the proxy passes through.
 *
 * An allow-list again. Anything unrecognized is dropped rather than forwarded, so a
 * future API parameter cannot be reached through the dashboard before anyone has
 * thought about whether it should be.
 */
const ALLOWED_QUERY_KEYS: ReadonlySet<string> = new Set([
  "name",
  "status",
  "git_commit",
  "since",
  "until",
  "min_duration_ms",
  "max_duration_ms",
  "has_errors",
  "limit",
  "cursor",
  // Experiment history filters by suite, which is how a reader asks "show me this suite's runs"
  // without paging through every other suite's.
  "suite_name",
  // The invitation being resolved. Dropped rather than forwarded, this parameter would turn every
  // preview into a 404 — an allow-list's characteristic failure, and one that looks like a broken
  // invitation rather than a missing entry here.
  "token",
])

export function filterQuery(params: URLSearchParams): URLSearchParams {
  const out = new URLSearchParams()
  for (const [key, value] of params) {
    if (ALLOWED_QUERY_KEYS.has(key)) out.append(key, value)
  }
  return out
}
