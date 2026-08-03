"use client"

import { formatDuration } from "@/lib/format"
import { type Span, type WaterfallRow, buildWaterfall, spanTypeColour } from "@/lib/spans"
import { useVirtualizer } from "@tanstack/react-virtual"
import { useMemo, useRef, useState } from "react"

const ROW_HEIGHT = 28
const INDENT_PX = 14

/**
 * The span waterfall.
 *
 * Virtualized because trace size is unbounded — an agent loop over a large document set
 * produces tens of thousands of spans, and rendering that many DOM nodes locks the tab
 * for seconds. The geometry itself lives in `lib/spans.ts` as pure functions; this
 * component only decides what is on screen.
 */
export function Waterfall({
  spans,
  selectedSpanId,
  onSelect,
}: {
  spans: Span[]
  selectedSpanId: string | null
  onSelect: (spanId: string) => void
}) {
  const rows = useMemo(() => buildWaterfall(spans), [spans])
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set())

  const visible = useMemo(() => hideCollapsedSubtrees(rows, collapsed), [rows, collapsed])

  const viewportRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => viewportRef.current,
    estimateSize: () => ROW_HEIGHT,
    // Enough overscan that a fast scroll does not show blank space, not so much that
    // it defeats the point.
    overscan: 12,
  })

  const toggle = (spanId: string): void => {
    setCollapsed((previous) => {
      const next = new Set(previous)
      if (next.has(spanId)) next.delete(spanId)
      else next.add(spanId)
      return next
    })
  }

  return (
    <div
      ref={viewportRef}
      className="waterfall-viewport h-[calc(100vh-16rem)] overflow-auto"
      // A tree of spans is a tree; announcing it as one is most of the accessibility
      // story for this view.
      role="tree"
      aria-label="Span waterfall"
    >
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((item) => {
          const row = visible[item.index]
          if (!row) return null
          return (
            <div
              key={row.span.span_id}
              style={{
                position: "absolute",
                top: item.start,
                height: item.size,
                left: 0,
                right: 0,
              }}
            >
              <WaterfallRowView
                row={row}
                isSelected={row.span.span_id === selectedSpanId}
                isCollapsed={collapsed.has(row.span.span_id)}
                onSelect={() => onSelect(row.span.span_id)}
                onToggle={() => toggle(row.span.span_id)}
              />
            </div>
          )
        })}
      </div>
    </div>
  )
}

/**
 * Drop rows inside a collapsed subtree.
 *
 * Done by depth rather than by walking parent links: the rows are already in
 * depth-first order, so one pass suffices, and it handles nested collapses without
 * needing to know which ancestor caused the hiding.
 */
export function hideCollapsedSubtrees(
  rows: readonly WaterfallRow[],
  collapsed: ReadonlySet<string>,
): WaterfallRow[] {
  const out: WaterfallRow[] = []
  let hiddenBelowDepth: number | null = null

  for (const row of rows) {
    if (hiddenBelowDepth !== null) {
      if (row.depth > hiddenBelowDepth) continue
      hiddenBelowDepth = null
    }
    out.push(row)
    if (collapsed.has(row.span.span_id) && row.hasChildren) hiddenBelowDepth = row.depth
  }
  return out
}

function WaterfallRowView({
  row,
  isSelected,
  isCollapsed,
  onSelect,
  onToggle,
}: {
  row: WaterfallRow
  isSelected: boolean
  isCollapsed: boolean
  onSelect: () => void
  onToggle: () => void
}) {
  const { span } = row
  const isError = span.status === "error" || span.status === "timeout"

  return (
    <div
      role="treeitem"
      aria-level={row.depth + 1}
      aria-selected={isSelected}
      aria-expanded={row.hasChildren ? !isCollapsed : undefined}
      // A `treeitem` is interactive: it is selectable and expandable, and it must be
      // focusable for the arrow-key handling below to reach it. Biome's rule only sees
      // the `div`.
      // biome-ignore lint/a11y/noNoninteractiveTabindex: role="treeitem" is interactive
      tabIndex={0}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          onSelect()
        }
        if (event.key === "ArrowRight" && isCollapsed) onToggle()
        if (event.key === "ArrowLeft" && !isCollapsed && row.hasChildren) onToggle()
      }}
      className={`flex h-[28px] cursor-default items-center gap-2 px-2 text-xs ${
        isSelected ? "bg-slate-800" : "hover:bg-slate-800/40"
      }`}
    >
      <div
        className="flex min-w-0 shrink-0 items-center gap-1"
        style={{ width: 340, paddingLeft: row.depth * INDENT_PX }}
      >
        {row.hasChildren ? (
          <button
            type="button"
            aria-label={isCollapsed ? "Expand" : "Collapse"}
            onClick={(event) => {
              event.stopPropagation()
              onToggle()
            }}
            className="w-3 text-slate-500 hover:text-slate-200"
          >
            {isCollapsed ? "▸" : "▾"}
          </button>
        ) : (
          <span className="w-3" />
        )}

        <span className={`h-2 w-2 shrink-0 rounded-sm ${spanTypeColour(span.span_type)}`} />
        <span className={`truncate ${isError ? "text-red-300" : "text-slate-200"}`}>
          {span.name}
        </span>
        {row.isOrphan ? (
          // An orphan's parent was dropped, so its indentation is a guess. Saying so
          // beats drawing it at depth 0 as if it were a root.
          <span className="shrink-0 text-amber-400" title="Parent span is missing from this trace">
            ⚠
          </span>
        ) : null}
      </div>

      <div className="relative h-3 flex-1 rounded bg-slate-800/40">
        <div
          className={`absolute top-0 h-3 rounded ${
            isError ? "bg-red-500/70" : spanTypeColour(span.span_type)
          } ${row.isOpen ? "opacity-40" : ""}`}
          style={{ left: `${row.offset * 100}%`, width: `${row.width * 100}%` }}
          // The bar is decorative; the duration is in the next column as text.
          aria-hidden="true"
        />
      </div>

      <span className="w-20 shrink-0 text-right text-slate-400">
        {row.isOpen ? (
          <span title="This span never reported an end time">unfinished</span>
        ) : (
          formatDuration(span.duration_ms)
        )}
      </span>
    </div>
  )
}
