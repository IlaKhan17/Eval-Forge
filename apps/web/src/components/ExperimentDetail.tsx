"use client"

/**
 * One experiment's runs, and how its metrics moved between them.
 *
 * The comparison is against the *previous run of this experiment*, which is not the same thing as
 * the baseline a gate used — a gate compares against the latest run on the baseline branch, which
 * may be a different experiment entirely. Saying "vs previous run" rather than "delta" is the whole
 * difference between a number someone can act on and one they will misread as a gate verdict.
 */

import { ErrorState, Panel, Skeleton } from "@/components/Primitives"
import { getRunMetrics, listRuns } from "@/lib/api"
import { type RunOutcome, compareMetrics, errorRate, outcomeOf, sortRuns } from "@/lib/experiments"
import { formatCost, formatDuration, formatTimestamp, shortId } from "@/lib/format"
import { useQuery } from "@tanstack/react-query"
import Link from "next/link"
import { useState } from "react"

export function ExperimentDetailView({ experimentId }: { experimentId: string }) {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)

  const runsQuery = useQuery({
    queryKey: ["runs", experimentId],
    queryFn: ({ signal }) => listRuns(experimentId, signal),
  })

  const runs = runsQuery.data ? sortRuns(runsQuery.data) : []
  const selected = runs.find((run) => run.id === selectedRunId) ?? runs[0]
  // The run immediately older than the selected one. `undefined` on the oldest run, which is what
  // makes "no previous run" render as an em dash rather than as a delta against nothing.
  const previous = selected ? runs[runs.indexOf(selected) + 1] : undefined

  const metricsQuery = useQuery({
    queryKey: ["run-metrics", selected?.id],
    queryFn: ({ signal }) => getRunMetrics(selected?.id ?? "", signal),
    enabled: Boolean(selected),
  })
  const previousQuery = useQuery({
    queryKey: ["run-metrics", previous?.id],
    queryFn: ({ signal }) => getRunMetrics(previous?.id ?? "", signal),
    enabled: Boolean(previous),
  })

  if (runsQuery.isPending) return <Skeleton rows={12} />
  if (runsQuery.isError) {
    return <ErrorState error={runsQuery.error} onRetry={() => runsQuery.refetch()} />
  }

  // `isPending` is not "still loading" for a *disabled* query — React Query reports a query that
  // was never enabled as pending forever. Gating on it hid the metrics table permanently on the
  // oldest run, which is precisely the run with no previous to compare against. `isFetching`, and
  // only when there is actually a previous run to wait for.
  const waitingForPrevious = Boolean(previous) && previousQuery.isFetching
  const comparisons =
    metricsQuery.data && !waitingForPrevious
      ? compareMetrics(metricsQuery.data, previousQuery.data ?? [])
      : []

  return (
    <div className="space-y-4">
      <div>
        <Link href="/experiments" className="text-xs text-slate-400 hover:text-slate-200">
          ← Experiments
        </Link>
        <h1 className="mt-1 font-mono text-lg font-medium text-slate-100">
          {shortId(experimentId, 12)}
        </h1>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Panel title={`Runs · ${runs.length}`}>
          {runs.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm text-slate-400">
              This experiment has no runs. It was created but never executed.
            </p>
          ) : (
            <ul className="divide-y divide-slate-800">
              {runs.map((run) => (
                <li key={run.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedRunId(run.id)}
                    className={`w-full px-4 py-3 text-left text-xs hover:bg-slate-900/60 ${
                      run.id === selected?.id ? "bg-slate-900/80" : ""
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <OutcomeBadge outcome={outcomeOf(run)} />
                      <span className="text-slate-300">attempt {run.attempt}</span>
                      <span className="text-slate-500">{formatTimestamp(run.started_at)}</span>
                    </div>
                    <div className="mt-1 text-slate-500">
                      {run.completed_examples} completed
                      {/*
                        Failures shown separately and only when there are any. An errored example
                        is not a scored one — folding the two together is exactly the arithmetic
                        this project refuses to do elsewhere, and it would be no better here.
                      */}
                      {run.failed_examples > 0 ? (
                        <span className="text-amber-300">
                          {" "}
                          · {run.failed_examples} failed ({formatRate(errorRate(run))})
                        </span>
                      ) : null}
                      {" · "}
                      {formatCost(run.total_cost)}
                      {run.started_at && run.ended_at
                        ? ` · ${formatDuration(Date.parse(run.ended_at) - Date.parse(run.started_at))}`
                        : null}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel
          title={
            previous
              ? `Metrics · vs previous run ${shortId(previous.id, 6)}`
              : "Metrics · first run, nothing to compare"
          }
        >
          {metricsQuery.isPending ? (
            <Skeleton rows={8} />
          ) : metricsQuery.isError ? (
            <ErrorState error={metricsQuery.error} onRetry={() => metricsQuery.refetch()} />
          ) : comparisons.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm text-slate-400">
              This run produced no metrics. It may have failed before any evaluator ran.
            </p>
          ) : (
            <MetricTable comparisons={comparisons} />
          )}
        </Panel>
      </div>
    </div>
  )
}

function MetricTable({
  comparisons,
}: {
  comparisons: ReturnType<typeof compareMetrics>
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-slate-500">
          <tr className="border-b border-slate-800">
            <th className="px-4 py-2 text-left font-normal">Metric</th>
            <th className="px-4 py-2 text-right font-normal">Value</th>
            <th className="px-4 py-2 text-right font-normal">Previous</th>
            <th className="px-4 py-2 text-right font-normal">Δ</th>
            <th className="px-4 py-2 text-right font-normal">n</th>
          </tr>
        </thead>
        <tbody>
          {comparisons.map((row) => (
            <tr key={row.fullKey} className="border-b border-slate-900">
              <td className="px-4 py-1.5 font-mono text-slate-300">{row.fullKey}</td>
              <td className="px-4 py-1.5 text-right text-slate-200">{formatValue(row.value)}</td>
              <td className="px-4 py-1.5 text-right text-slate-500">{formatValue(row.previous)}</td>
              <td className={`px-4 py-1.5 text-right ${deltaTone(row.delta)}`}>
                {formatDelta(row.delta)}
              </td>
              <td className="px-4 py-1.5 text-right text-slate-500">
                {row.count}
                {/*
                  Errored evaluations counted beside the sample size, never inside it. A metric
                  measured over 8 of 40 examples is a different claim from one measured over 40,
                  and the mean alone cannot tell you which you are looking at.
                */}
                {row.errorCount > 0 ? (
                  <span className="text-amber-300"> +{row.errorCount} err</span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function OutcomeBadge({ outcome }: { outcome: RunOutcome }) {
  const tone =
    outcome === "succeeded"
      ? "border-emerald-900/60 bg-emerald-950/30 text-emerald-200"
      : outcome === "failed"
        ? "border-red-900/60 bg-red-950/30 text-red-200"
        : "border-slate-700 bg-slate-900 text-slate-300"
  return (
    <span className={`rounded border px-1.5 py-0.5 font-medium uppercase ${tone}`}>{outcome}</span>
  )
}

/** An em dash for absent, never a zero — see the note at the top of lib/experiments.ts. */
function formatValue(value: number | null): string {
  if (value === null) return "—"
  return Number.isInteger(value) ? String(value) : value.toFixed(4)
}

function formatDelta(delta: number | null): string {
  if (delta === null) return "—"
  if (delta === 0) return "0"
  return `${delta > 0 ? "+" : ""}${delta.toFixed(4)}`
}

function formatRate(rate: number | null): string {
  return rate === null ? "—" : `${(rate * 100).toFixed(0)}%`
}

/**
 * Colour by direction only, never by "good" or "bad".
 *
 * Up is better for accuracy and worse for cost, and this table does not know which metric it is
 * looking at. Painting a cost increase green would be worse than painting it nothing at all.
 */
function deltaTone(delta: number | null): string {
  if (delta === null || delta === 0) return "text-slate-500"
  return delta > 0 ? "text-sky-300" : "text-amber-300"
}
