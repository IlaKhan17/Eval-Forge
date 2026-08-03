/**
 * Trace-list filters, encoded in the URL.
 *
 * The URL is the state. A filtered trace list has to be linkable — the whole point of
 * one is pasting it into an incident channel — so filters live in query params rather
 * than in component state, and the parsing is pure so it can be tested without a
 * router.
 *
 * Parsing is *lenient in, strict out*: an unparseable param is dropped rather than
 * throwing. A stale link from a Slack message six months ago should still show a trace
 * list, not an error page. The one thing never done silently is narrowing — a dropped
 * filter widens the result set, so nothing is hidden by leniency.
 */

export type StatusFilter = "ok" | "error" | "timeout" | "unset"

export interface TraceFilters {
  name?: string
  status?: StatusFilter
  git_commit?: string
  since?: string
  until?: string
  min_duration_ms?: number
  max_duration_ms?: number
  has_errors?: boolean
  limit: number
  cursor?: string
}

export const DEFAULT_LIMIT = 50
const MAX_LIMIT = 200
const STATUSES: readonly StatusFilter[] = ["ok", "error", "timeout", "unset"]

export function parseFilters(params: URLSearchParams): TraceFilters {
  const filters: TraceFilters = { limit: parseLimit(params.get("limit")) }

  const name = params.get("name")?.trim()
  if (name) filters.name = name

  const status = params.get("status")?.trim().toLowerCase()
  if (status && (STATUSES as readonly string[]).includes(status)) {
    filters.status = status as StatusFilter
  }

  const commit = params.get("git_commit")?.trim()
  // A commit filter has to be a commit. Passing arbitrary text through would match
  // nothing and read as "no traces yet", which is a misleading empty state.
  if (commit && /^[0-9a-f]{7,40}$/i.test(commit)) filters.git_commit = commit.toLowerCase()

  // Ranges are resolved before assignment rather than assigned and then repaired, so
  // the object is never briefly in the inverted state. An inverted range yields
  // nothing, and an empty list is indistinguishable from "no data" — dropping the
  // upper bound is the reading that shows something.
  const since = parseInstant(params.get("since"))
  const until = parseInstant(params.get("until"))
  if (since) filters.since = since
  if (until && !(since && since > until)) filters.until = until

  const min = parseNonNegativeInt(params.get("min_duration_ms"))
  const max = parseNonNegativeInt(params.get("max_duration_ms"))
  if (min !== undefined) filters.min_duration_ms = min
  if (max !== undefined && !(min !== undefined && min > max)) filters.max_duration_ms = max

  const hasErrors = params.get("has_errors")
  if (hasErrors === "true" || hasErrors === "1") filters.has_errors = true
  else if (hasErrors === "false" || hasErrors === "0") filters.has_errors = false

  const cursor = params.get("cursor")?.trim()
  if (cursor) filters.cursor = cursor

  return filters
}

function parseLimit(raw: string | null): number {
  const value = Number(raw)
  if (!Number.isInteger(value) || value < 1) return DEFAULT_LIMIT
  return Math.min(value, MAX_LIMIT)
}

function parseNonNegativeInt(raw: string | null): number | undefined {
  if (raw === null || raw.trim() === "") return undefined
  const value = Number(raw)
  if (!Number.isFinite(value) || value < 0) return undefined
  return Math.floor(value)
}

/** Accept an ISO instant, or a relative shorthand like `-24h` used by the presets. */
function parseInstant(raw: string | null, now = Date.now()): string | undefined {
  if (!raw) return undefined
  const trimmed = raw.trim()

  const relative = /^-(\d+)([mhd])$/.exec(trimmed)
  if (relative) {
    const amount = Number(relative[1])
    const unit = relative[2]
    const scale = unit === "m" ? 60_000 : unit === "h" ? 3_600_000 : 86_400_000
    return new Date(now - amount * scale).toISOString()
  }

  const ms = Date.parse(trimmed)
  return Number.isNaN(ms) ? undefined : new Date(ms).toISOString()
}

/**
 * Serialize filters back to a query string.
 *
 * Keys are emitted in a fixed order so the same filter set always produces the same
 * URL — otherwise the browser history fills with entries that differ only in
 * parameter order, and React Query caches the same request under several keys.
 */
export function serializeFilters(filters: TraceFilters): string {
  const params = new URLSearchParams()
  const put = (key: string, value: string | number | boolean | undefined): void => {
    if (value !== undefined && value !== "") params.set(key, String(value))
  }

  put("name", filters.name)
  put("status", filters.status)
  put("git_commit", filters.git_commit)
  put("since", filters.since)
  put("until", filters.until)
  put("min_duration_ms", filters.min_duration_ms)
  put("max_duration_ms", filters.max_duration_ms)
  put("has_errors", filters.has_errors)
  if (filters.limit !== DEFAULT_LIMIT) put("limit", filters.limit)
  put("cursor", filters.cursor)

  return params.toString()
}

/**
 * Change a filter, dropping the cursor.
 *
 * A cursor is only meaningful against the filter set that produced it — the server
 * decodes it into a `(started_at, id)` anchor and applies it to whatever query it is
 * handed. Carrying one across a filter change would silently start the list partway
 * into a result set it never belonged to.
 */
export function withFilter<K extends keyof TraceFilters>(
  filters: TraceFilters,
  key: K,
  value: TraceFilters[K],
): TraceFilters {
  // Rebuilt by filtering entries rather than by assigning `undefined`. A key present
  // with an undefined value is not the same as an absent key: `serializeFilters` would
  // skip it either way, but `parseFilters` output would no longer round-trip to an
  // equal object, and equality is what the query cache keys on.
  const dropCursor = key !== "cursor"
  const clearing = value === undefined || value === ""

  const entries = Object.entries(filters).filter(([existing]) => {
    if (existing === key) return false
    if (existing === "cursor" && dropCursor) return false
    return true
  })
  if (!clearing) entries.push([key, value])

  return Object.fromEntries(entries) as TraceFilters
}

export function isFiltered(filters: TraceFilters): boolean {
  return Boolean(
    filters.name ||
      filters.status ||
      filters.git_commit ||
      filters.since ||
      filters.until ||
      filters.min_duration_ms !== undefined ||
      filters.max_duration_ms !== undefined ||
      filters.has_errors !== undefined,
  )
}

/** Stable React Query key. Cursor included: a page is a distinct resource. */
export function queryKey(filters: TraceFilters): readonly unknown[] {
  return ["traces", serializeFilters(filters)]
}
