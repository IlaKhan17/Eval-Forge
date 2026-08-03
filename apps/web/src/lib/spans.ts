/**
 * Span tree construction and waterfall geometry.
 *
 * Pure functions, deliberately separate from any component. Two reasons: the
 * geometry is the part that can be *wrong* in a way that misleads someone reading a
 * trace, and a pure function is testable without mounting React or a browser.
 *
 * The waterfall is what people use to answer "what took so long" and "what ran
 * before what". A bar in the wrong place is worse than no bar, so the ordering and
 * offset rules here mirror the trajectory engine's normalization exactly.
 */

export type SpanType =
  | "agent"
  | "workflow"
  | "llm"
  | "tool"
  | "retriever"
  | "embedding"
  | "guardrail"
  | "evaluator"
  | "custom"

export type SpanStatus = "ok" | "error" | "timeout" | "unset"

export interface Span {
  span_id: string
  parent_span_id: string | null
  name: string
  span_type: SpanType
  status: SpanStatus
  status_message: string | null
  started_at: string
  ended_at: string | null
  duration_ms: number | null
  attributes: Record<string, unknown>
  input?: unknown
  output?: unknown
  tool_args?: unknown
  input_truncated?: boolean
  output_truncated?: boolean
  model: string | null
  provider: string | null
  total_tokens: number
  cost: number | null
  tool_name: string | null
  error_type: string | null
  sequence_index: number
  events: Array<{ name: string; timestamp: string; attributes: Record<string, unknown> }>
}

export interface TraceDetail {
  trace_id: string
  name: string
  status: SpanStatus
  started_at: string
  ended_at: string | null
  duration_ms: number | null
  span_count: number
  error_count: number
  total_tokens: number
  total_cost: number
  dropped_span_count: number
  git_commit: string | null
  metadata: Record<string, unknown>
  tags: Record<string, unknown>
  state: Record<string, unknown>
  spans: Span[]
  orphan_span_ids: string[]
}

/** A span placed in the tree, with the geometry needed to draw one row. */
export interface WaterfallRow {
  span: Span
  depth: number
  /** Fraction of the trace window where this span starts, 0–1. */
  offset: number
  /** Fraction of the trace window this span occupies, 0–1. */
  width: number
  hasChildren: boolean
  /** True when the parent id names a span that is not in this trace. */
  isOrphan: boolean
  /** True when the span never ended, so its extent is unknown rather than zero. */
  isOpen: boolean
}

const MIN_VISIBLE_WIDTH = 0.002

export function toMillis(timestamp: string): number {
  return Date.parse(timestamp)
}

/**
 * Order spans the way the trajectory engine does.
 *
 * By *start* time, ties broken on the SDK's monotonic counter and then span id. Two
 * reasons this matters beyond aesthetics: a span that begins a side effect earlier
 * must appear earlier, and clock granularity makes identical timestamps common — so
 * without the tiebreak the same trace could render in a different order on each load.
 */
export function sortSpans(spans: readonly Span[]): Span[] {
  return [...spans].sort((a, b) => {
    const byTime = toMillis(a.started_at) - toMillis(b.started_at)
    if (byTime !== 0) return byTime
    if (a.sequence_index !== b.sequence_index) return a.sequence_index - b.sequence_index
    return a.span_id.localeCompare(b.span_id)
  })
}

/**
 * The time window the waterfall is drawn against.
 *
 * Derived from the spans rather than from the trace's own timestamps: a trace whose
 * root closed early, or whose `ended_at` is missing, would otherwise clip or squash
 * every bar. An open span extends the window to the latest known start.
 */
export function traceWindow(spans: readonly Span[]): { start: number; end: number } {
  if (spans.length === 0) {
    const now = Date.now()
    return { start: now, end: now + 1 }
  }

  let start = Number.POSITIVE_INFINITY
  let end = Number.NEGATIVE_INFINITY
  for (const span of spans) {
    const spanStart = toMillis(span.started_at)
    const spanEnd = span.ended_at ? toMillis(span.ended_at) : spanStart
    if (spanStart < start) start = spanStart
    if (spanEnd > end) end = spanEnd
  }
  // A trace where everything happened within the same millisecond still needs a
  // non-zero window, or every width becomes NaN.
  return { start, end: end > start ? end : start + 1 }
}

/**
 * Flatten spans into ordered rows with depth and geometry.
 *
 * Depth-first from each root, children in start order, so the visual nesting matches
 * the causal nesting. Orphans — a parent id naming a span not present, which happens
 * whenever the exporter dropped one — are hoisted to depth 0 and flagged rather than
 * silently discarded.
 */
export function buildWaterfall(spans: readonly Span[]): WaterfallRow[] {
  if (spans.length === 0) return []

  const ordered = sortSpans(spans)
  const byId = new Map(ordered.map((span) => [span.span_id, span]))
  const children = new Map<string, Span[]>()
  const roots: Span[] = []

  for (const span of ordered) {
    const parentId = span.parent_span_id
    if (parentId && byId.has(parentId)) {
      const siblings = children.get(parentId)
      if (siblings) siblings.push(span)
      else children.set(parentId, [span])
    } else {
      roots.push(span)
    }
  }

  const { start, end } = traceWindow(ordered)
  const span_ms = end - start
  const rows: WaterfallRow[] = []
  const visited = new Set<string>()

  // An explicit stack rather than recursion. Span depth is bounded only by what the
  // instrumented application does, and an agent that recurses — a planner calling a
  // planner — can nest thousands deep. A recursive walker turns that into a stack
  // overflow, which in a browser means a blank page rather than a partial one.
  const walk = (entry: Span, entryDepth: number): void => {
    const stack: Array<{ span: Span; depth: number }> = [{ span: entry, depth: entryDepth }]

    while (stack.length > 0) {
      const frame = stack.pop()
      if (!frame) break
      const { span, depth } = frame

      // A cycle in parent links should never happen, but a malformed trace must render
      // rather than hang the tab.
      if (visited.has(span.span_id)) continue
      visited.add(span.span_id)

      const spanStart = toMillis(span.started_at)
      const isOpen = span.ended_at === null
      const spanEnd = isOpen ? end : toMillis(span.ended_at as string)

      rows.push({
        span,
        depth,
        offset: clamp((spanStart - start) / span_ms),
        // Floor the width so a sub-millisecond span is still visible. A bar of zero
        // width reads as "did not happen".
        width: Math.max(MIN_VISIBLE_WIDTH, clamp((spanEnd - spanStart) / span_ms)),
        hasChildren: (children.get(span.span_id)?.length ?? 0) > 0,
        isOrphan: Boolean(span.parent_span_id) && !byId.has(span.parent_span_id as string),
        isOpen,
      })

      // Pushed in reverse so the stack pops them in start order — depth-first output
      // has to match the order the children were sorted into.
      const kids = children.get(span.span_id)
      if (kids) {
        for (let i = kids.length - 1; i >= 0; i -= 1) {
          const child = kids[i]
          if (child) stack.push({ span: child, depth: depth + 1 })
        }
      }
    }
  }

  for (const root of roots) walk(root, 0)

  // Anything unreachable from a root (a parent cycle) still gets rendered.
  for (const span of ordered) if (!visited.has(span.span_id)) walk(span, 0)

  return rows
}

function clamp(value: number): number {
  if (!Number.isFinite(value)) return 0
  return Math.min(1, Math.max(0, value))
}

/** Collapse a subtree, returning the ids to hide. */
export function collapsedDescendants(rows: readonly WaterfallRow[], spanId: string): Set<string> {
  const hidden = new Set<string>()
  const index = rows.findIndex((row) => row.span.span_id === spanId)
  if (index === -1) return hidden

  const parentDepth = rows[index]?.depth ?? 0
  for (let i = index + 1; i < rows.length; i += 1) {
    const row = rows[i]
    if (!row || row.depth <= parentDepth) break
    hidden.add(row.span.span_id)
  }
  return hidden
}

/** Self time: a span's duration minus time accounted for by its children. */
export function selfTimeMs(span: Span, spans: readonly Span[]): number | null {
  if (span.duration_ms === null) return null

  const children = spans.filter((candidate) => candidate.parent_span_id === span.span_id)
  if (children.length === 0) return span.duration_ms

  // Merge overlapping child intervals before subtracting. Summing child durations
  // would double-count concurrent work and can produce a negative self time, which
  // is the kind of number that makes people distrust the whole view.
  const intervals = children
    .map((child) => ({
      from: toMillis(child.started_at),
      to: child.ended_at ? toMillis(child.ended_at) : toMillis(child.started_at),
    }))
    .sort((a, b) => a.from - b.from)

  let covered = 0
  let cursor = Number.NEGATIVE_INFINITY
  for (const interval of intervals) {
    const from = Math.max(interval.from, cursor)
    if (interval.to > from) {
      covered += interval.to - from
      cursor = interval.to
    }
  }
  return Math.max(0, span.duration_ms - covered)
}

export function spanTypeColour(type: SpanType): string {
  const palette: Record<SpanType, string> = {
    agent: "bg-span-agent",
    workflow: "bg-span-workflow",
    llm: "bg-span-llm",
    tool: "bg-span-tool",
    retriever: "bg-span-retriever",
    embedding: "bg-span-embedding",
    guardrail: "bg-span-guardrail",
    evaluator: "bg-span-evaluator",
    custom: "bg-span-custom",
  }
  return palette[type] ?? palette.custom
}
