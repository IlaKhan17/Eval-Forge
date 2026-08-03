/**
 * Display formatting.
 *
 * The rule throughout: never make a number look more precise than it is, and never
 * let a missing value render as a real one. `null` cost is not `$0.00` — one means
 * "not measured" and the other means "free", and confusing them in a cost view is
 * how someone concludes their agent is cheap when it was simply not instrumented.
 */

const EM_DASH = "—"

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return EM_DASH
  if (ms < 1) return "<1ms"
  if (ms < 1_000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1_000).toFixed(2)}s`

  const minutes = Math.floor(ms / 60_000)
  const seconds = Math.round((ms % 60_000) / 1_000)
  return `${minutes}m ${seconds}s`
}

export function formatCost(cost: number | null | undefined): string {
  if (cost === null || cost === undefined || !Number.isFinite(cost)) return EM_DASH
  if (cost === 0) return "$0"
  // Sub-cent costs are the common case for a single span. Rounding them to two
  // decimals turns every span into $0.00 and makes the column useless.
  if (cost < 0.01) return `$${cost.toFixed(6)}`
  if (cost < 1) return `$${cost.toFixed(4)}`
  return `$${cost.toFixed(2)}`
}

export function formatTokens(tokens: number | null | undefined): string {
  if (tokens === null || tokens === undefined || !Number.isFinite(tokens)) return EM_DASH
  if (tokens < 1_000) return String(tokens)
  if (tokens < 1_000_000) return `${(tokens / 1_000).toFixed(1)}k`
  return `${(tokens / 1_000_000).toFixed(2)}M`
}

/**
 * A score, shown to three decimals.
 *
 * Fixed width rather than trimmed: a column of scores is read by scanning down it,
 * and ragged decimals defeat that.
 */
export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EM_DASH
  return value.toFixed(3)
}

/** A signed delta, where the sign carries the meaning. */
export function formatDelta(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EM_DASH
  if (value === 0) return "0.000"
  return `${value > 0 ? "+" : "-"}${Math.abs(value).toFixed(3)}`
}

export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return EM_DASH
  const ms = Date.parse(iso)
  if (Number.isNaN(ms)) return EM_DASH
  return new Date(ms).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })
}

/**
 * Relative age, for the trace list.
 *
 * `now` is a parameter rather than a call to `Date.now()` so the function is pure and
 * testable, and so a server-rendered page and its hydration agree on the value.
 */
export function formatRelative(iso: string | null | undefined, now: number): string {
  if (!iso) return EM_DASH
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return EM_DASH

  const seconds = Math.round((now - then) / 1_000)
  if (seconds < 0) return "just now"
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)}h ago`
  if (seconds < 2_592_000) return `${Math.floor(seconds / 86_400)}d ago`
  return formatTimestamp(iso)
}

/** Shorten an id for display while keeping enough to distinguish two of them. */
export function shortId(id: string, keep = 8): string {
  return id.length <= keep ? id : id.slice(0, keep)
}

export function formatCount(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`
}
