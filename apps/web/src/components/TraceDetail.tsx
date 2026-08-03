"use client"

import { ErrorState, Panel, Skeleton, StatusBadge } from "@/components/Primitives"
import { SpanInspector } from "@/components/SpanInspector"
import { Waterfall } from "@/components/Waterfall"
import { getTrace } from "@/lib/api"
import { formatCost, formatDuration, formatTimestamp, formatTokens } from "@/lib/format"
import { useQuery } from "@tanstack/react-query"
import Link from "next/link"
import { useState } from "react"

export function TraceDetailView({ traceId }: { traceId: string }) {
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null)

  const query = useQuery({
    queryKey: ["trace", traceId],
    queryFn: ({ signal }) => getTrace(traceId, signal),
  })

  if (query.isPending) return <Skeleton rows={14} />
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />

  const trace = query.data
  const selected = trace.spans.find((span) => span.span_id === selectedSpanId) ?? trace.spans[0]

  return (
    <div className="space-y-4">
      <div>
        <Link href="/traces" className="text-xs text-slate-400 hover:text-slate-200">
          ← Traces
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-medium text-slate-100">{trace.name}</h1>
          <StatusBadge status={trace.status} />
          <span className="font-mono text-xs text-slate-500">{trace.trace_id}</span>
        </div>
      </div>

      <dl className="grid grid-cols-2 gap-4 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3 text-xs sm:grid-cols-4 lg:grid-cols-7">
        <Stat label="Started" value={formatTimestamp(trace.started_at)} />
        <Stat label="Duration" value={formatDuration(trace.duration_ms)} />
        <Stat label="Spans" value={String(trace.span_count)} />
        <Stat label="Errors" value={String(trace.error_count)} emphasis={trace.error_count > 0} />
        <Stat label="Tokens" value={formatTokens(trace.total_tokens)} />
        <Stat label="Cost" value={formatCost(trace.total_cost)} />
        <Stat label="Commit" value={trace.git_commit ? trace.git_commit.slice(0, 7) : "—"} />
      </dl>

      {/*
        Two ways this view can be lying, both stated rather than hidden. Dropped spans
        mean the SDK's queue overflowed; orphans mean a parent is missing. Either way the
        waterfall is incomplete, and a gap in a trace is exactly the sort of thing that
        gets misread as "the agent did not call that tool".
      */}
      {trace.dropped_span_count > 0 ? (
        <Notice>
          {trace.dropped_span_count} span(s) were dropped before export, so this trace is
          incomplete. The SDK drops spans rather than blocking the application — raise{" "}
          <code>queue_size</code> or lower the capture mode if this recurs.
        </Notice>
      ) : null}
      {trace.orphan_span_ids.length > 0 ? (
        <Notice>
          {trace.orphan_span_ids.length} span(s) reference a parent that is not in this trace and
          are shown at the top level, marked ⚠. Their nesting is unknown, not flat.
        </Notice>
      ) : null}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Panel title={`Waterfall · ${trace.spans.length} spans`}>
          {trace.spans.length === 0 ? (
            <p className="px-4 py-8 text-center text-sm text-slate-400">
              This trace has no spans. It was created by an ingest that carried trace-level data
              only.
            </p>
          ) : (
            <Waterfall
              spans={trace.spans}
              selectedSpanId={selected?.span_id ?? null}
              onSelect={setSelectedSpanId}
            />
          )}
        </Panel>

        <Panel title="Span">
          {selected ? (
            <div className="max-h-[calc(100vh-16rem)]">
              <SpanInspector span={selected} spans={trace.spans} />
            </div>
          ) : (
            <p className="px-4 py-8 text-center text-sm text-slate-400">Select a span.</p>
          )}
        </Panel>
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  emphasis = false,
}: {
  label: string
  value: string
  emphasis?: boolean
}) {
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd className={emphasis ? "text-red-300" : "text-slate-200"}>{value}</dd>
    </div>
  )
}

function Notice({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded border border-amber-900/50 bg-amber-950/20 px-3 py-2 text-xs text-amber-200">
      {children}
    </p>
  )
}
