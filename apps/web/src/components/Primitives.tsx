import { ApiError } from "@/lib/api"
import type { SpanStatus } from "@/lib/spans"
import type { ReactNode } from "react"

export function StatusBadge({ status }: { status: SpanStatus | string }) {
  const styles: Record<string, string> = {
    ok: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
    error: "bg-red-500/15 text-red-300 ring-red-500/30",
    timeout: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
    // `unset` means the span never reported an outcome — usually a process that died.
    // Styled distinctly from `ok` on purpose; the two are often confused.
    unset: "bg-slate-500/15 text-slate-400 ring-slate-500/30",
  }
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${
        styles[status] ?? styles.unset
      }`}
    >
      {status}
    </span>
  )
}

export function Panel({
  title,
  action,
  children,
}: {
  title?: ReactNode
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40">
      {title ? (
        <header className="flex items-center justify-between border-b border-slate-800 px-4 py-2">
          <h2 className="text-sm font-medium text-slate-300">{title}</h2>
          {action}
        </header>
      ) : null}
      {children}
    </section>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="px-6 py-16 text-center">
      <p className="text-sm text-slate-300">{title}</p>
      {hint ? <p className="mt-2 text-xs text-slate-500">{hint}</p> : null}
    </div>
  )
}

/**
 * A failed request, explained.
 *
 * The API already writes a usable `detail` for every error it returns, so this shows it
 * verbatim rather than replacing it with "Something went wrong". Retry is offered only
 * when retrying could work — a button that cannot help is worse than no button, because
 * it invites clicking instead of reading.
 */
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const api = error instanceof ApiError ? error : null
  const message =
    api?.detail ?? (error instanceof Error ? error.message : "An unexpected error occurred.")

  return (
    <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-4 py-4">
      <p className="text-sm font-medium text-red-200">
        {api?.status ? `Error ${api.status}` : "Error"}
      </p>
      <p className="mt-1 text-sm text-red-100/90">{message}</p>
      {api?.requestId ? (
        <p className="mt-2 font-mono text-xs text-red-200/60">request {api.requestId}</p>
      ) : null}
      {onRetry && (api === null || api.isTransient) ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded border border-red-800 px-2 py-1 text-xs text-red-100 hover:bg-red-900/40"
        >
          Retry
        </button>
      ) : null}
    </div>
  )
}

export function Skeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-4" aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }, (_, index) => (
        // Placeholder bars are identical, hold no state, and never reorder, so an
        // index is the only identity they have.
        // biome-ignore lint/suspicious/noArrayIndexKey: static placeholder list
        <div key={index} className="h-6 animate-pulse rounded bg-slate-800/70" />
      ))}
    </div>
  )
}
