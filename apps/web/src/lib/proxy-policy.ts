/**
 * What the proxy is allowed to forward.
 *
 * The proxy holds a credential the browser does not have, which makes it a confused
 * deputy waiting to happen: anything it forwards, it forwards *with authority*. So the
 * rule is an allow-list of read-only endpoints, not "pass through whatever arrives".
 *
 * Concretely, without this a page on the dashboard's origin — or any site that can get
 * a request to it — could reach `POST /v1/experiments/{id}/promote-baseline` and change
 * which run future gates compare against. That is a quiet, high-impact write, and it
 * has no business being reachable from a read-only trace viewer.
 *
 * Pure and separately tested, because a mistake here is a security bug and route
 * handlers are awkward to test.
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
]

export interface PolicyDecision {
  allowed: boolean
  /** Reason, for the 403 body and the server log. Only set when denied. */
  reason?: string
}

export function checkProxyRequest(method: string, path: string): PolicyDecision {
  if (method !== "GET" && method !== "HEAD") {
    return {
      allowed: false,
      reason: `The dashboard proxy forwards read requests only; ${method} is not allowed. Writes go through the CLI or the API directly, with their own credential.`,
    }
  }

  if (!path.startsWith("/")) {
    return { allowed: false, reason: "Path must be absolute." }
  }
  // `..` cannot survive URL normalization in a browser, but this proxy can also be
  // called by anything else, and a traversal that escapes the allow-list would defeat
  // the entire mechanism.
  if (path.includes("..") || path.includes("//") || path.includes("\\")) {
    return { allowed: false, reason: "Path is not in a normalized form." }
  }

  if (!ALLOWED_GET_PATTERNS.some((pattern) => pattern.test(path))) {
    return { allowed: false, reason: `The dashboard proxy does not expose ${path}.` }
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
])

export function filterQuery(params: URLSearchParams): URLSearchParams {
  const out = new URLSearchParams()
  for (const [key, value] of params) {
    if (ALLOWED_QUERY_KEYS.has(key)) out.append(key, value)
  }
  return out
}
