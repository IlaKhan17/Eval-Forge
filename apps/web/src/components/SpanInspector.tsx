"use client"

import { PayloadViewer } from "@/components/PayloadViewer"
import { StatusBadge } from "@/components/Primitives"
import { formatCost, formatDuration, formatTimestamp, formatTokens, shortId } from "@/lib/format"
import { type Span, selfTimeMs } from "@/lib/spans"

/**
 * Everything known about one span.
 *
 * Ordered by what someone opening a span actually wants: why it failed, how long it
 * took and how much of that was its own, then what went in and came out, then the
 * attribute bag last. The error message goes first and uncollapsed — a span is usually
 * opened *because* it is red.
 */
export function SpanInspector({ span, spans }: { span: Span; spans: Span[] }) {
  const self = selfTimeMs(span, spans)
  const isError = span.status === "error" || span.status === "timeout"

  return (
    <div className="space-y-3 overflow-y-auto p-3 text-sm">
      <header>
        <div className="flex items-center gap-2">
          <StatusBadge status={span.status} />
          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-xs text-slate-300">
            {span.span_type}
          </span>
        </div>
        <h3 className="mt-2 break-words font-medium text-slate-100">{span.name}</h3>
        <p className="mt-1 font-mono text-xs text-slate-500">
          {shortId(span.span_id, 16)}
          {span.parent_span_id ? ` ← ${shortId(span.parent_span_id, 16)}` : " (root)"}
        </p>
      </header>

      {isError ? (
        <div className="rounded border border-red-900/60 bg-red-950/30 px-3 py-2">
          <p className="text-xs font-medium text-red-200">{span.error_type ?? span.status}</p>
          {span.status_message ? (
            <p className="mt-1 whitespace-pre-wrap break-words text-xs text-red-100/90">
              {span.status_message}
            </p>
          ) : null}
        </div>
      ) : null}

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
        <Field label="Started" value={formatTimestamp(span.started_at)} />
        <Field label="Duration" value={formatDuration(span.duration_ms)} />
        <Field
          label="Self time"
          value={formatDuration(self)}
          // Worth explaining once: a span whose duration is large but whose self time is
          // near zero is waiting on its children, not doing work.
          hint="This span's duration minus the time covered by its children, with overlapping children counted once."
        />
        <Field label="Ended" value={span.ended_at ? formatTimestamp(span.ended_at) : "never"} />
        {span.model ? <Field label="Model" value={span.model} /> : null}
        {span.provider ? <Field label="Provider" value={span.provider} /> : null}
        {span.tool_name ? <Field label="Tool" value={span.tool_name} /> : null}
        <Field label="Tokens" value={formatTokens(span.total_tokens)} />
        <Field label="Cost" value={formatCost(span.cost)} />
      </dl>

      {span.tool_args !== undefined && span.tool_args !== null ? (
        <PayloadViewer label="Tool arguments" value={span.tool_args} />
      ) : null}
      <PayloadViewer label="Input" value={span.input} serverTruncated={span.input_truncated} />
      <PayloadViewer label="Output" value={span.output} serverTruncated={span.output_truncated} />

      {span.events.length > 0 ? (
        <div className="rounded border border-slate-800">
          <header className="border-b border-slate-800 px-3 py-1.5 text-xs font-medium uppercase tracking-wide text-slate-500">
            Events ({span.events.length})
          </header>
          <ul className="divide-y divide-slate-800/60">
            {span.events.map((event, index) => (
              <li key={`${event.name}-${event.timestamp}-${index}`} className="px-3 py-1.5 text-xs">
                <span className="text-slate-200">{event.name}</span>
                <span className="ml-2 text-slate-500">{formatTimestamp(event.timestamp)}</span>
                {Object.keys(event.attributes).length > 0 ? (
                  <pre className="payload-body mt-1 text-slate-400">
                    {JSON.stringify(event.attributes, null, 2)}
                  </pre>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {Object.keys(span.attributes).length > 0 ? (
        <PayloadViewer label="Attributes" value={span.attributes} />
      ) : null}
    </div>
  )
}

function Field({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <dt className="text-slate-500" title={hint}>
        {label}
        {hint ? <span className="ml-1 cursor-help text-slate-600">ⓘ</span> : null}
      </dt>
      <dd className="break-words text-slate-200">{value}</dd>
    </div>
  )
}
