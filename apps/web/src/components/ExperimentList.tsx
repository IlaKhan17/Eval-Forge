"use client"

/**
 * CI history: what the gates have decided, over time.
 *
 * Until publishing landed, a run existed only as an exit code in a CI log and a JSON file in an
 * artifact bucket. Now it is a record — and a record nobody can look at is barely better than the
 * log line it replaced. This is the view that closes that loop.
 *
 * Grouped by suite rather than listed flat, because a suite is the unit people think in ("how is
 * `checkout-agent` doing?") while an experiment is one invocation of it.
 */

import { EmptyState, ErrorState, Panel, Skeleton } from "@/components/Primitives"
import { listExperiments } from "@/lib/api"
import { type Experiment, bySuite } from "@/lib/experiments"
import { shortId } from "@/lib/format"
import { useQuery } from "@tanstack/react-query"
import Link from "next/link"

export function ExperimentList() {
  const query = useQuery({
    queryKey: ["experiments"],
    queryFn: ({ signal }) => listExperiments(undefined, signal),
  })

  if (query.isPending) return <Skeleton rows={8} />
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />

  const experiments = query.data
  if (experiments.length === 0) {
    return (
      <EmptyState
        title="No experiments yet"
        hint={
          <>
            Runs appear here once the CLI publishes them. Set <code>EVALFORGE_ENDPOINT</code> and{" "}
            <code>EVALFORGE_API_KEY</code> in the job that runs <code>evalforge eval</code>; without
            them the run still gates, it just is not recorded.
          </>
        }
      />
    )
  }

  const grouped = bySuite(experiments)

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-medium text-slate-100">Experiments</h1>
        <p className="mt-1 text-xs text-slate-400">
          Every published run, with the dataset it was measured against.
        </p>
      </div>

      {[...grouped.entries()].map(([suite, runs]) => (
        <Panel key={suite} title={`${suite} · ${runs.length}`}>
          <ul className="divide-y divide-slate-800">
            {runs.map((experiment) => (
              <ExperimentRow key={experiment.id} experiment={experiment} />
            ))}
          </ul>
        </Panel>
      ))}
    </div>
  )
}

function ExperimentRow({ experiment }: { experiment: Experiment }) {
  return (
    <li className="px-4 py-3 text-xs">
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href={`/experiments/${experiment.id}`}
          className="font-mono text-slate-200 underline-offset-2 hover:underline"
        >
          {shortId(experiment.id)}
        </Link>
        {experiment.git_branch ? (
          <span className="text-slate-400">{experiment.git_branch}</span>
        ) : null}
        {experiment.git_commit ? (
          <span className="font-mono text-slate-500">{experiment.git_commit.slice(0, 7)}</span>
        ) : null}
        {experiment.is_baseline ? (
          <span className="rounded border border-sky-900/60 bg-sky-950/30 px-1.5 py-0.5 text-sky-200">
            baseline
          </span>
        ) : null}
      </div>

      {/*
        The dataset hash, not just its id. Two runs of the same suite are only comparable if they
        measured the same data, and the hash is what makes that checkable rather than assumed —
        it is the same value the gate engine refuses to compare across.
      */}
      <div className="mt-1 text-slate-500">
        {experiment.dataset_content_hash ? (
          <>dataset sha {experiment.dataset_content_hash.slice(0, 12)}</>
        ) : (
          <span className="text-amber-300">no dataset recorded — nothing to compare against</span>
        )}
      </div>
    </li>
  )
}
