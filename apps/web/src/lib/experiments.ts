/**
 * Shaping CI history for reading.
 *
 * The API returns experiments, runs, and metrics as three flat lists. Turning those into "did this
 * suite get better or worse, and when" is the work — and it is pure, so it is here rather than
 * inside a component where it could only be tested through the DOM.
 *
 * One judgement runs through the whole file: **a missing number is not a zero.** A run that failed
 * before it produced a metric, a metric that only exists on one side of a comparison, a cost nobody
 * recorded — every one of those renders as an em dash, never as 0. A dashboard that shows 0 for
 * "unknown" is how someone concludes a metric collapsed when in fact it was never measured.
 */

export interface Experiment {
  id: string
  name: string
  suite_name: string
  dataset_version_id: string | null
  dataset_content_hash: string | null
  git_commit: string | null
  git_branch: string | null
  is_baseline: boolean
}

export interface Run {
  id: string
  experiment_id: string
  attempt: number
  status: string
  completed_examples: number
  failed_examples: number
  total_cost: number
  started_at: string | null
  ended_at: string | null
}

export interface Metric {
  key: string
  slice: Record<string, string> | null
  value: number | null
  count: number
  error_count: number
  ci_low: number | null
  ci_high: number | null
}

/** A metric with the same key and slice in the run before it. */
export interface MetricComparison {
  key: string
  slice: Record<string, string> | null
  fullKey: string
  value: number | null
  previous: number | null
  /** `null` when either side is missing — not 0, which would read as "no change". */
  delta: number | null
  errorCount: number
  count: number
}

/**
 * Key including slice, matching how the API and the CLI both spell it.
 *
 * Two metrics with the same key and different slices are different measurements — per-class recall
 * for `unsubscribe` and for `meeting` are the whole point of protected gates — so anything that
 * matches runs against each other has to compare both parts or it silently pairs the wrong rows.
 */
export function fullKey(key: string, slice: Record<string, string> | null): string {
  if (!slice || Object.keys(slice).length === 0) return key
  const inner = Object.entries(slice)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join(",")
  return `${key}[${inner}]`
}

/** Runs newest first, with unstarted ones last rather than sorted as if they began at the epoch. */
export function sortRuns(runs: readonly Run[]): Run[] {
  return [...runs].sort((a, b) => {
    if (!a.started_at && !b.started_at) return 0
    if (!a.started_at) return 1
    if (!b.started_at) return -1
    return Date.parse(b.started_at) - Date.parse(a.started_at)
  })
}

/**
 * Pair a run's metrics against the previous run's.
 *
 * Sorted by key so the table is stable between renders — a list that reorders as values change is
 * unreadable precisely when it matters, which is while something is moving.
 */
export function compareMetrics(
  current: readonly Metric[],
  previous: readonly Metric[],
): MetricComparison[] {
  const before = new Map(previous.map((metric) => [fullKey(metric.key, metric.slice), metric]))

  return current
    .map((metric) => {
      const key = fullKey(metric.key, metric.slice)
      const prior = before.get(key)
      const previousValue = prior?.value ?? null
      return {
        key: metric.key,
        slice: metric.slice,
        fullKey: key,
        value: metric.value,
        previous: previousValue,
        // Only when both sides exist. A first run has nothing to compare against, and calling that
        // a delta of 0 would report "unchanged" for a measurement that has never been taken before.
        delta:
          metric.value !== null && previousValue !== null ? metric.value - previousValue : null,
        errorCount: metric.error_count,
        count: metric.count,
      }
    })
    .sort((a, b) => a.fullKey.localeCompare(b.fullKey))
}

/**
 * Did this run succeed, fail, or not finish?
 *
 * Derived from the run's own status rather than from its metrics, because a run that crashed
 * half-way can still have perfectly good metrics for the examples it completed — and showing those
 * as a healthy result is exactly the kind of quiet lie this project is built against.
 */
export type RunOutcome = "succeeded" | "failed" | "cancelled" | "running" | "unknown"

export function outcomeOf(run: Run): RunOutcome {
  switch (run.status) {
    case "succeeded":
      return "succeeded"
    case "failed":
      return "failed"
    case "cancelled":
      return "cancelled"
    case "running":
    case "pending":
      return "running"
    default:
      return "unknown"
  }
}

/** Share of examples that errored, or `null` when the run completed nothing. */
export function errorRate(run: Run): number | null {
  const total = run.completed_examples + run.failed_examples
  return total > 0 ? run.failed_examples / total : null
}

/**
 * Group experiments by the suite that produced them.
 *
 * A suite is the unit people think in — "how is `checkout-agent` doing" — while an experiment is one
 * invocation of it. Sorted by suite name, and each group keeps the API's newest-first order.
 */
export function bySuite(experiments: readonly Experiment[]): Map<string, Experiment[]> {
  const grouped = new Map<string, Experiment[]>()
  for (const experiment of experiments) {
    const existing = grouped.get(experiment.suite_name)
    if (existing) existing.push(experiment)
    else grouped.set(experiment.suite_name, [experiment])
  }
  return new Map([...grouped.entries()].sort(([a], [b]) => a.localeCompare(b)))
}
