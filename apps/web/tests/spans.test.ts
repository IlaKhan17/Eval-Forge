import { hideCollapsedSubtrees } from "@/components/Waterfall"
import {
  type Span,
  buildWaterfall,
  collapsedDescendants,
  selfTimeMs,
  sortSpans,
  traceWindow,
} from "@/lib/spans"
import { describe, expect, it } from "vitest"

const BASE = Date.parse("2026-01-01T00:00:00.000Z")

function span(overrides: Partial<Span> & { span_id: string }): Span {
  const startMs = overrides.started_at ? Date.parse(overrides.started_at) : BASE
  return {
    parent_span_id: null,
    name: overrides.span_id,
    span_type: "custom",
    status: "ok",
    status_message: null,
    started_at: new Date(startMs).toISOString(),
    ended_at: new Date(startMs + 100).toISOString(),
    duration_ms: 100,
    attributes: {},
    model: null,
    provider: null,
    total_tokens: 0,
    cost: null,
    tool_name: null,
    error_type: null,
    sequence_index: 0,
    events: [],
    ...overrides,
  }
}

/** Offsets by span id, so assertions read as claims about a span rather than an index. */
function geometry(spans: Span[]): Record<string, { offset: number; width: number; depth: number }> {
  return Object.fromEntries(
    buildWaterfall(spans).map((row) => [
      row.span.span_id,
      { offset: row.offset, width: row.width, depth: row.depth },
    ]),
  )
}

describe("sortSpans", () => {
  it("orders by start time", () => {
    const spans = [
      span({ span_id: "b", started_at: new Date(BASE + 50).toISOString() }),
      span({ span_id: "a", started_at: new Date(BASE).toISOString() }),
    ]
    expect(sortSpans(spans).map((s) => s.span_id)).toEqual(["a", "b"])
  })

  it("breaks ties on sequence_index, not on array order", () => {
    // Same-millisecond starts are the common case, not an edge case: a tool call and
    // its retry can easily land in one millisecond. Without the tiebreak the render
    // order would depend on however the rows came back from the database.
    const spans = [
      span({ span_id: "second", sequence_index: 7 }),
      span({ span_id: "first", sequence_index: 3 }),
    ]
    expect(sortSpans(spans).map((s) => s.span_id)).toEqual(["first", "second"])
  })

  it("is deterministic when even sequence_index ties", () => {
    const spans = [span({ span_id: "zz" }), span({ span_id: "aa" })]
    expect(sortSpans(spans).map((s) => s.span_id)).toEqual(["aa", "zz"])
    expect(sortSpans([...spans].reverse()).map((s) => s.span_id)).toEqual(["aa", "zz"])
  })

  it("does not mutate its input", () => {
    const spans = [
      span({ span_id: "b", started_at: new Date(BASE + 5).toISOString() }),
      span({ span_id: "a" }),
    ]
    sortSpans(spans)
    expect(spans.map((s) => s.span_id)).toEqual(["b", "a"])
  })
})

describe("traceWindow", () => {
  it("spans from the earliest start to the latest end", () => {
    const window = traceWindow([
      span({ span_id: "a", started_at: new Date(BASE).toISOString() }),
      span({
        span_id: "b",
        started_at: new Date(BASE + 500).toISOString(),
        ended_at: new Date(BASE + 900).toISOString(),
      }),
    ])
    expect(window).toEqual({ start: BASE, end: BASE + 900 })
  })

  it("never returns a zero-width window", () => {
    // Everything inside one millisecond would otherwise divide by zero and turn every
    // bar's width into NaN, which renders as no bar at all.
    const window = traceWindow([
      span({ span_id: "a", ended_at: new Date(BASE).toISOString(), duration_ms: 0 }),
    ])
    expect(window.end).toBeGreaterThan(window.start)
  })

  it("does not clip a child that outlives its parent", () => {
    // A fire-and-forget task closes after the span that started it. Deriving the window
    // from the root's end time would push that child off the right edge.
    const window = traceWindow([
      span({
        span_id: "root",
        ended_at: new Date(BASE + 100).toISOString(),
      }),
      span({
        span_id: "child",
        parent_span_id: "root",
        started_at: new Date(BASE + 50).toISOString(),
        ended_at: new Date(BASE + 400).toISOString(),
      }),
    ])
    expect(window.end).toBe(BASE + 400)
  })
})

describe("buildWaterfall", () => {
  it("nests children under their parent in start order", () => {
    const rows = buildWaterfall([
      span({ span_id: "root" }),
      span({
        span_id: "second",
        parent_span_id: "root",
        started_at: new Date(BASE + 60).toISOString(),
      }),
      span({
        span_id: "first",
        parent_span_id: "root",
        started_at: new Date(BASE + 20).toISOString(),
      }),
    ])
    expect(rows.map((row) => [row.span.span_id, row.depth])).toEqual([
      ["root", 0],
      ["first", 1],
      ["second", 1],
    ])
  })

  it("computes offset and width as fractions of the trace window", () => {
    const geo = geometry([
      span({ span_id: "root", ended_at: new Date(BASE + 1000).toISOString(), duration_ms: 1000 }),
      span({
        span_id: "half",
        parent_span_id: "root",
        started_at: new Date(BASE + 500).toISOString(),
        ended_at: new Date(BASE + 750).toISOString(),
      }),
    ])
    expect(geo.root).toMatchObject({ offset: 0, width: 1 })
    expect(geo.half?.offset).toBeCloseTo(0.5, 10)
    expect(geo.half?.width).toBeCloseTo(0.25, 10)
  })

  it("hoists an orphan to the top level and flags it", () => {
    // The exporter dropped the parent. Discarding the child would remove evidence that
    // a tool ran at all — the worst failure mode for a debugging view.
    const rows = buildWaterfall([span({ span_id: "child", parent_span_id: "vanished" })])
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ depth: 0, isOrphan: true })
  })

  it("gives a sub-millisecond span a visible width", () => {
    const geo = geometry([
      span({
        span_id: "long",
        ended_at: new Date(BASE + 10_000).toISOString(),
        duration_ms: 10_000,
      }),
      span({
        span_id: "instant",
        parent_span_id: "long",
        started_at: new Date(BASE + 5_000).toISOString(),
        ended_at: new Date(BASE + 5_000).toISOString(),
        duration_ms: 0,
      }),
    ])
    // A zero-width bar reads as "did not happen", which is a different claim.
    expect(geo.instant?.width).toBeGreaterThan(0)
  })

  it("draws an unfinished span to the end of the window and marks it open", () => {
    const rows = buildWaterfall([
      span({ span_id: "open", ended_at: null, duration_ms: null }),
      span({
        span_id: "later",
        started_at: new Date(BASE + 400).toISOString(),
        ended_at: new Date(BASE + 500).toISOString(),
      }),
    ])
    const open = rows.find((row) => row.span.span_id === "open")
    expect(open?.isOpen).toBe(true)
    expect(open?.width).toBeCloseTo(1, 10)
  })

  it("renders every span even when parent links form a cycle", () => {
    // Should be impossible, but a malformed trace must not hang the tab, and it must not
    // silently show fewer spans than the count in the header.
    const rows = buildWaterfall([
      span({ span_id: "a", parent_span_id: "b" }),
      span({ span_id: "b", parent_span_id: "a" }),
    ])
    expect(rows.map((row) => row.span.span_id).sort()).toEqual(["a", "b"])
  })

  it("returns nothing for an empty trace", () => {
    expect(buildWaterfall([])).toEqual([])
  })

  it("handles a deep chain without recursion blowing the stack", () => {
    const spans = Array.from({ length: 5_000 }, (_, index) =>
      span({
        span_id: `s${index}`,
        parent_span_id: index === 0 ? null : `s${index - 1}`,
        started_at: new Date(BASE + index).toISOString(),
      }),
    )
    // A 5,000-deep chain is not realistic, but a 5,000-deep *recursion* is what a naive
    // walker does with it, and an agent loop can nest far further than anyone expects.
    expect(buildWaterfall(spans)).toHaveLength(5_000)
  })
})

describe("selfTimeMs", () => {
  it("subtracts a single child", () => {
    const parent = span({
      span_id: "p",
      ended_at: new Date(BASE + 100).toISOString(),
      duration_ms: 100,
    })
    const child = span({
      span_id: "c",
      parent_span_id: "p",
      started_at: new Date(BASE + 10).toISOString(),
      ended_at: new Date(BASE + 40).toISOString(),
      duration_ms: 30,
    })
    expect(selfTimeMs(parent, [parent, child])).toBe(70)
  })

  it("counts overlapping children once", () => {
    // Two concurrent tool calls of 80ms each inside a 100ms parent. Summing durations
    // would give 160ms of children and a negative self time — a number that makes the
    // whole view look broken.
    const parent = span({
      span_id: "p",
      ended_at: new Date(BASE + 100).toISOString(),
      duration_ms: 100,
    })
    const children = [
      span({
        span_id: "a",
        parent_span_id: "p",
        started_at: new Date(BASE + 10).toISOString(),
        ended_at: new Date(BASE + 90).toISOString(),
        duration_ms: 80,
      }),
      span({
        span_id: "b",
        parent_span_id: "p",
        started_at: new Date(BASE + 15).toISOString(),
        ended_at: new Date(BASE + 95).toISOString(),
        duration_ms: 80,
      }),
    ]
    expect(selfTimeMs(parent, [parent, ...children])).toBe(15)
  })

  it("never returns a negative value", () => {
    const parent = span({
      span_id: "p",
      ended_at: new Date(BASE + 10).toISOString(),
      duration_ms: 10,
    })
    const child = span({
      span_id: "c",
      parent_span_id: "p",
      started_at: new Date(BASE).toISOString(),
      ended_at: new Date(BASE + 500).toISOString(),
      duration_ms: 500,
    })
    expect(selfTimeMs(parent, [parent, child])).toBe(0)
  })

  it("is unknown, not zero, for an unfinished span", () => {
    const parent = span({ span_id: "p", ended_at: null, duration_ms: null })
    expect(selfTimeMs(parent, [parent])).toBeNull()
  })
})

describe("collapse", () => {
  const spans = [
    span({ span_id: "root" }),
    span({
      span_id: "branch",
      parent_span_id: "root",
      started_at: new Date(BASE + 10).toISOString(),
    }),
    span({
      span_id: "leaf",
      parent_span_id: "branch",
      started_at: new Date(BASE + 20).toISOString(),
    }),
    span({
      span_id: "sibling",
      parent_span_id: "root",
      started_at: new Date(BASE + 30).toISOString(),
    }),
  ]

  it("collapsedDescendants stops at the first shallower row", () => {
    expect(collapsedDescendants(buildWaterfall(spans), "branch")).toEqual(new Set(["leaf"]))
  })

  it("hides a whole subtree but keeps later siblings", () => {
    const visible = hideCollapsedSubtrees(buildWaterfall(spans), new Set(["branch"]))
    expect(visible.map((row) => row.span.span_id)).toEqual(["root", "branch", "sibling"])
  })

  it("collapsing the root hides everything below it", () => {
    const visible = hideCollapsedSubtrees(buildWaterfall(spans), new Set(["root"]))
    expect(visible.map((row) => row.span.span_id)).toEqual(["root"])
  })

  it("ignores a collapsed leaf", () => {
    const visible = hideCollapsedSubtrees(buildWaterfall(spans), new Set(["leaf"]))
    expect(visible).toHaveLength(4)
  })
})

describe("scale", () => {
  it("builds a 10,000-span waterfall quickly", () => {
    // The size that motivated virtualization. This asserts the *pure* layout pass stays
    // cheap; if it ever goes quadratic, no amount of virtualization saves the view.
    const spans = Array.from({ length: 10_000 }, (_, index) =>
      span({
        span_id: `s${index}`,
        parent_span_id: index === 0 ? null : `s${Math.floor(index / 4)}`,
        started_at: new Date(BASE + index).toISOString(),
      }),
    )

    const started = performance.now()
    const rows = buildWaterfall(spans)
    const elapsed = performance.now() - started

    expect(rows).toHaveLength(10_000)
    // Generous by design: this is a regression guard against an accidental O(n²), not a
    // benchmark, and CI machines vary.
    expect(elapsed).toBeLessThan(1_000)
  })
})
