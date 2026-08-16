"use client"

import { EmptyState, ErrorState, Panel, Skeleton, StatusBadge } from "@/components/Primitives"
import { listTraces } from "@/lib/api"
import {
  type StatusFilter,
  type TraceFilters,
  isFiltered,
  parseFilters,
  queryKey,
  serializeFilters,
  withFilter,
} from "@/lib/filters"
import { formatCost, formatDuration, formatRelative, formatTokens, shortId } from "@/lib/format"
import { useQuery } from "@tanstack/react-query"
import Link from "next/link"
import { useRouter, useSearchParams } from "next/navigation"
import { useCallback, useMemo, useState } from "react"

export function TraceList() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const filters = useMemo(
    () => parseFilters(new URLSearchParams(searchParams.toString())),
    [searchParams],
  )

  // Captured once per render rather than called inside each row: `Date.now()` in a
  // render body makes the output non-deterministic and the relative times inconsistent
  // down the list.
  const [now] = useState(() => Date.now())

  const query = useQuery({
    queryKey: queryKey(filters),
    queryFn: ({ signal }) => listTraces(filters, signal),
  })

  const apply = useCallback(
    (next: TraceFilters) => {
      const search = serializeFilters(next)
      router.push(search ? `/traces?${search}` : "/traces")
    },
    [router],
  )

  return (
    <div className="space-y-4">
      <FilterBar filters={filters} onChange={apply} />

      <Panel
        title={
          query.data
            ? `${query.data.data.length}${query.data.has_more ? "+" : ""} traces`
            : "Traces"
        }
      >
        {query.isPending ? <Skeleton /> : null}
        {query.isError ? (
          <div className="p-4">
            <ErrorState error={query.error} onRetry={() => query.refetch()} />
          </div>
        ) : null}

        {query.data && query.data.data.length === 0 ? (
          <EmptyState
            title={isFiltered(filters) ? "No traces match these filters." : "No traces yet."}
            hint={
              isFiltered(filters) ? (
                <button
                  type="button"
                  className="underline"
                  onClick={() => apply({ limit: filters.limit })}
                >
                  Clear filters
                </button>
              ) : (
                // The empty state has to distinguish "nothing sent yet" from "nothing
                // matched", because the fix is completely different.
                <>
                  Instrument an application with the SDK, or run <code>proofstep eval</code>.
                </>
              )
            }
          />
        ) : null}

        {query.data && query.data.data.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
                <tr className="border-b border-slate-800">
                  <th className="px-4 py-2 font-medium">Trace</th>
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="px-4 py-2 font-medium">Started</th>
                  <th className="px-4 py-2 text-right font-medium">Duration</th>
                  <th className="px-4 py-2 text-right font-medium">Spans</th>
                  <th className="px-4 py-2 text-right font-medium">Errors</th>
                  <th className="px-4 py-2 text-right font-medium">Tokens</th>
                  <th className="px-4 py-2 text-right font-medium">Cost</th>
                  <th className="px-4 py-2 font-medium">Commit</th>
                </tr>
              </thead>
              <tbody>
                {query.data.data.map((trace) => (
                  <tr
                    key={trace.trace_id}
                    className="border-b border-slate-800/60 hover:bg-slate-800/30"
                  >
                    <td className="max-w-[24rem] truncate px-4 py-2">
                      <Link
                        href={`/traces/${encodeURIComponent(trace.trace_id)}`}
                        className="text-slate-100 hover:underline"
                      >
                        {trace.name}
                      </Link>
                      <span className="ml-2 font-mono text-xs text-slate-500">
                        {shortId(trace.trace_id)}
                      </span>
                    </td>
                    <td className="px-4 py-2">
                      <StatusBadge status={trace.status} />
                    </td>
                    <td className="px-4 py-2 text-slate-400">
                      {formatRelative(trace.started_at, now)}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-300">
                      {formatDuration(trace.duration_ms)}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-300">
                      {trace.span_count}
                      {/* Dropped spans mean the view is incomplete. Saying so in the
                          list matters: a missing span changes what the waterfall and
                          the trajectory verdict can be trusted to show. */}
                      {trace.dropped_span_count > 0 ? (
                        <span
                          className="ml-1 text-amber-400"
                          title={`${trace.dropped_span_count} span(s) dropped by the exporter`}
                        >
                          −{trace.dropped_span_count}
                        </span>
                      ) : null}
                    </td>
                    <td className="px-4 py-2 text-right">
                      {trace.error_count > 0 ? (
                        <span className="text-red-300">{trace.error_count}</span>
                      ) : (
                        <span className="text-slate-600">0</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-400">
                      {formatTokens(trace.total_tokens)}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-400">
                      {formatCost(trace.total_cost)}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-slate-500">
                      {trace.git_commit ? shortId(trace.git_commit, 7) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {query.data?.has_more && query.data.next_cursor ? (
          <div className="border-t border-slate-800 px-4 py-3 text-right">
            {/* Cursor pagination is forward-only, so there is no "previous". Offering a
                page number would imply random access the API deliberately does not
                provide — OFFSET on the trace table is a full scan. */}
            <button
              type="button"
              onClick={() =>
                apply(withFilter(filters, "cursor", query.data.next_cursor ?? undefined))
              }
              className="rounded border border-slate-700 px-3 py-1 text-xs text-slate-200 hover:bg-slate-800"
            >
              Next page →
            </button>
          </div>
        ) : null}
      </Panel>
    </div>
  )
}

const STATUS_OPTIONS: ReadonlyArray<StatusFilter | ""> = ["", "ok", "error", "timeout", "unset"]

function FilterBar({
  filters,
  onChange,
}: {
  filters: TraceFilters
  onChange: (next: TraceFilters) => void
}) {
  return (
    <div className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3">
      <label className="flex flex-col gap-1 text-xs text-slate-400">
        Name
        <input
          type="text"
          defaultValue={filters.name ?? ""}
          placeholder="exact trace name"
          // Applied on Enter or blur rather than per keystroke: each change is a URL
          // push and a request, and per-keystroke history entries make the back button
          // useless.
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              onChange(withFilter(filters, "name", event.currentTarget.value.trim() || undefined))
            }
          }}
          onBlur={(event) => {
            const value = event.currentTarget.value.trim() || undefined
            if (value !== filters.name) onChange(withFilter(filters, "name", value))
          }}
          className="w-56 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-100"
        />
      </label>

      <label className="flex flex-col gap-1 text-xs text-slate-400">
        Status
        <select
          value={filters.status ?? ""}
          onChange={(event) =>
            onChange(
              withFilter(filters, "status", (event.target.value || undefined) as StatusFilter),
            )
          }
          className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-100"
        >
          {STATUS_OPTIONS.map((option) => (
            <option key={option || "any"} value={option}>
              {option || "any"}
            </option>
          ))}
        </select>
      </label>

      <label className="flex items-center gap-2 text-xs text-slate-400">
        <input
          type="checkbox"
          checked={filters.has_errors === true}
          onChange={(event) =>
            onChange(withFilter(filters, "has_errors", event.target.checked ? true : undefined))
          }
        />
        Errors only
      </label>

      {isFiltered(filters) ? (
        <button
          type="button"
          onClick={() => onChange({ limit: filters.limit })}
          className="ml-auto text-xs text-slate-400 underline hover:text-slate-200"
        >
          Clear
        </button>
      ) : null}
    </div>
  )
}
