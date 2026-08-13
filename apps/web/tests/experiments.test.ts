import {
  type Experiment,
  type Metric,
  type Run,
  bySuite,
  compareMetrics,
  errorRate,
  fullKey,
  outcomeOf,
  sortRuns,
} from "@/lib/experiments"
import { describe, expect, it } from "vitest"

/**
 * Shaping CI history.
 *
 * One rule runs through every case here: a missing number is not a zero. That is the difference
 * between "this metric was not measured" and "this metric collapsed", and a history view that
 * confuses them sends someone to investigate a regression that never happened.
 */

function run(overrides: Partial<Run> & { id: string }): Run {
  return {
    experiment_id: "exp",
    attempt: 1,
    status: "succeeded",
    completed_examples: 10,
    failed_examples: 0,
    total_cost: 0,
    started_at: "2026-01-01T00:00:00Z",
    ended_at: "2026-01-01T00:01:00Z",
    ...overrides,
  }
}

function metric(overrides: Partial<Metric> & { key: string }): Metric {
  return {
    slice: null,
    value: 1,
    count: 10,
    error_count: 0,
    ci_low: null,
    ci_high: null,
    ...overrides,
  }
}

describe("fullKey", () => {
  it("distinguishes slices of the same metric", () => {
    // Per-class recall for two classes is two measurements. Collapsing them would pair the wrong
    // rows in a comparison — and protected-class gating is the reason slices exist at all.
    expect(fullKey("recall", { class: "unsubscribe" })).toBe("recall[class=unsubscribe]")
    expect(fullKey("recall", { class: "meeting" })).not.toBe(fullKey("recall", { class: "x" }))
  })

  it("is stable regardless of key order in the slice", () => {
    // JSON object order is not guaranteed across a round trip, and an unstable key would make a
    // metric appear to vanish and reappear between runs.
    expect(fullKey("m", { b: "2", a: "1" })).toBe(fullKey("m", { a: "1", b: "2" }))
  })

  it("leaves an unsliced metric alone", () => {
    expect(fullKey("accuracy", null)).toBe("accuracy")
    expect(fullKey("accuracy", {})).toBe("accuracy")
  })
})

describe("compareMetrics", () => {
  it("pairs a metric with the same key and slice", () => {
    const [row] = compareMetrics(
      [metric({ key: "accuracy", value: 0.9 })],
      [metric({ key: "accuracy", value: 0.8 })],
    )
    expect(row).toBeDefined()
    if (!row) return
    expect(row.value).toBe(0.9)
    expect(row.previous).toBe(0.8)
    expect(row.delta).toBeCloseTo(0.1)
  })

  it("does not invent a delta when there is no previous run", () => {
    // The first run of a suite. A delta of 0 here would report "unchanged" for a measurement that
    // has never been taken before.
    const [row] = compareMetrics([metric({ key: "accuracy", value: 0.9 })], [])
    expect(row).toBeDefined()
    if (!row) return
    expect(row.previous).toBeNull()
    expect(row.delta).toBeNull()
  })

  it("does not invent a delta when a value is missing", () => {
    // A metric can exist with no value — an evaluator that errored on every example produces a
    // count and no mean. Treating that as 0 would show a total collapse.
    const [row] = compareMetrics(
      [metric({ key: "accuracy", value: null, error_count: 10, count: 0 })],
      [metric({ key: "accuracy", value: 0.8 })],
    )
    expect(row).toBeDefined()
    if (!row) return
    expect(row.value).toBeNull()
    expect(row.delta).toBeNull()
    expect(row.errorCount).toBe(10)
  })

  it("does not pair different slices", () => {
    const rows = compareMetrics(
      [metric({ key: "recall", slice: { class: "unsubscribe" }, value: 0 })],
      [metric({ key: "recall", slice: { class: "meeting" }, value: 1 })],
    )
    expect(rows).toHaveLength(1)
    expect(rows[0]?.previous).toBeNull()
  })

  it("orders rows stably", () => {
    // A table that reorders as values change is unreadable exactly when something is moving.
    const rows = compareMetrics(
      [metric({ key: "zebra" }), metric({ key: "alpha" }), metric({ key: "middle" })],
      [],
    )
    expect(rows.map((r) => r.key)).toEqual(["alpha", "middle", "zebra"])
  })
})

describe("sortRuns", () => {
  it("puts the newest run first", () => {
    const sorted = sortRuns([
      run({ id: "old", started_at: "2026-01-01T00:00:00Z" }),
      run({ id: "new", started_at: "2026-02-01T00:00:00Z" }),
    ])
    expect(sorted.map((r) => r.id)).toEqual(["new", "old"])
  })

  it("puts runs that never started last, not first", () => {
    // A null date sorts as the epoch under a naive comparator, which would put a run that never
    // began at the top of a list ordered by recency.
    const sorted = sortRuns([
      run({ id: "never", started_at: null }),
      run({ id: "started", started_at: "2026-01-01T00:00:00Z" }),
    ])
    expect(sorted.map((r) => r.id)).toEqual(["started", "never"])
  })
})

describe("outcomeOf", () => {
  it("reads the run's own status rather than guessing from metrics", () => {
    // A run that crashed half-way still has good metrics for what it completed. Deriving success
    // from those would present a partial run as a healthy one.
    expect(outcomeOf(run({ id: "a", status: "failed", completed_examples: 100 }))).toBe("failed")
    expect(outcomeOf(run({ id: "b", status: "succeeded" }))).toBe("succeeded")
    expect(outcomeOf(run({ id: "c", status: "running" }))).toBe("running")
    expect(outcomeOf(run({ id: "d", status: "something-new" }))).toBe("unknown")
  })
})

describe("errorRate", () => {
  it("is null rather than zero when nothing ran", () => {
    expect(errorRate(run({ id: "a", completed_examples: 0, failed_examples: 0 }))).toBeNull()
  })

  it("is the share of attempted examples that failed", () => {
    expect(errorRate(run({ id: "a", completed_examples: 9, failed_examples: 1 }))).toBeCloseTo(0.1)
  })
})

describe("bySuite", () => {
  it("groups experiments under the suite people think in", () => {
    const experiments: Experiment[] = [
      { suite_name: "b", id: "1" },
      { suite_name: "a", id: "2" },
      { suite_name: "b", id: "3" },
    ].map((partial) => ({
      name: partial.suite_name,
      dataset_version_id: null,
      dataset_content_hash: null,
      git_commit: null,
      git_branch: null,
      is_baseline: false,
      ...partial,
    }))

    const grouped = bySuite(experiments)
    expect([...grouped.keys()]).toEqual(["a", "b"])
    expect(grouped.get("b")?.map((e) => e.id)).toEqual(["1", "3"])
  })
})
