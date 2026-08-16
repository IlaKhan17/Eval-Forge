import { TraceDetailView } from "@/components/TraceDetail"
import type { Span, TraceDetail } from "@/lib/spans"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

/**
 * Rendering tests for the trace detail view.
 *
 * The pure modules are covered elsewhere; what these cover is the wiring that a unit
 * test of a pure function cannot reach — that the fetch actually happens against the
 * same-origin proxy path, that a failure surfaces the API's own message, and that the
 * two "this view is incomplete" warnings appear when they should. Those warnings are
 * the ones most worth pinning: silently rendering a partial trace as if it were whole
 * is the failure mode that leads someone to conclude a tool was never called.
 */

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

function trace(overrides: Partial<TraceDetail> = {}): TraceDetail {
  return {
    trace_id: "abc123",
    name: "reply-drafter",
    status: "ok",
    started_at: new Date(BASE).toISOString(),
    ended_at: new Date(BASE + 300).toISOString(),
    duration_ms: 300,
    span_count: 2,
    error_count: 0,
    total_tokens: 1_200,
    total_cost: 0.004_2,
    dropped_span_count: 0,
    git_commit: "abcdef1234567890",
    metadata: {},
    tags: {},
    state: {},
    spans: [
      span({ span_id: "root", span_type: "agent" }),
      span({
        span_id: "child",
        parent_span_id: "root",
        span_type: "llm",
        started_at: new Date(BASE + 20).toISOString(),
      }),
    ],
    orphan_span_ids: [],
    ...overrides,
  }
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    // Retries would make a failure test wait for backoff, and there is nothing
    // transient about a stubbed response.
    defaultOptions: { queries: { retry: false } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function stubFetch(response: { status?: number; body: unknown; contentType?: string }): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(response.body), {
          status: response.status ?? 200,
          headers: { "content-type": response.contentType ?? "application/json" },
        }),
    ),
  )
}

beforeEach(() => {
  // jsdom has no layout engine and no ResizeObserver, so the virtualizer measures a
  // zero-height viewport and renders no rows. Both have to be stubbed for the waterfall
  // to be observable at all; without them this file would silently assert nothing about
  // the rows.
  // `offsetHeight` specifically: that is what @tanstack/react-virtual measures the
  // scroll element with, and jsdom reports 0 for it.
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 600 })
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", { configurable: true, value: 1_200 })
  Object.defineProperty(HTMLElement.prototype, "getBoundingClientRect", {
    configurable: true,
    value: () => ({ width: 1_200, height: 600, top: 0, left: 0, bottom: 600, right: 1_200 }),
  })
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(target: Element): void {
        // Fired synchronously: the virtualizer only computes a range once it has a
        // rect, and nothing else in jsdom will ever deliver one.
        this.callback(
          [{ target, contentRect: target.getBoundingClientRect() } as ResizeObserverEntry],
          this as unknown as ResizeObserver,
        )
      }
      unobserve(): void {}
      disconnect(): void {}
    },
  )
})

afterEach(() => {
  // Explicit, because Testing Library only auto-cleans when vitest runs with `globals: true`, and
  // this project does not. Without it every render accumulates in the same document and a query for
  // anything two traces share — the name, a status badge — matches several elements and throws. The
  // existing tests happened to query only unique strings, so the leak was invisible.
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("TraceDetailView", () => {
  it("fetches through the same-origin proxy, not the API host", async () => {
    // The point of the proxy is that the API key never reaches the browser. If this
    // component ever called the API directly, that guarantee would be gone.
    stubFetch({ body: trace() })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() => expect(screen.getByText("reply-drafter")).toBeDefined())
    const call = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(String(call?.[0])).toBe("/api/ef/v1/traces/abc123")
  })

  it("renders the span rows", async () => {
    stubFetch({ body: trace() })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() => expect(screen.getByRole("tree")).toBeDefined())
    const rows = await waitFor(() => {
      const found = screen.getAllByRole("treeitem")
      expect(found.length).toBeGreaterThan(0)
      return found
    })
    expect(rows).toHaveLength(2)
  })

  it("warns when spans were dropped", async () => {
    stubFetch({ body: trace({ dropped_span_count: 3 }) })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() => expect(screen.getByText(/were dropped before export/)).toBeDefined())
  })

  it("warns when a span's parent is missing", async () => {
    stubFetch({ body: trace({ orphan_span_ids: ["child"] }) })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() =>
      expect(screen.getByText(/reference a parent that is not in this trace/)).toBeDefined(),
    )
  })

  it("shows the API's own problem detail rather than a generic message", async () => {
    // The server already wrote a usable explanation. Replacing it with "Something went
    // wrong" throws away the only actionable part of the response.
    stubFetch({
      status: 403,
      contentType: "application/problem+json",
      body: {
        type: "https://proofstep.dev/problems/forbidden",
        title: "Forbidden",
        status: 403,
        detail: "This credential cannot read traces; it needs the 'read' scope.",
      },
    })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() => expect(screen.getByText(/it needs the 'read' scope/)).toBeDefined())
    // 403 is not transient, so no retry button — a button that cannot help invites
    // clicking instead of reading.
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull()
  })

  it("offers a retry on a server error", async () => {
    stubFetch({ status: 503, body: { detail: "The API failed to handle this request." } })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() => expect(screen.getByRole("button", { name: "Retry" })).toBeDefined())
  })

  it("renders a trace that has no spans at all", async () => {
    stubFetch({ body: trace({ spans: [], span_count: 0 }) })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() => expect(screen.getByText(/has no spans/)).toBeDefined())
  })
})

describe("policy verdicts", () => {
  it("shows nothing when no rule evaluated the trace", async () => {
    // A permanent empty panel would train people to ignore the area where the failures appear.
    stubFetch({ body: trace() })
    render(<TraceDetailView traceId="abc123" />, { wrapper })
    await waitFor(() => expect(screen.getByRole("heading", { name: "reply-drafter" })).toBeTruthy())
    expect(screen.queryByText(/evaluation\(s\)/)).toBeNull()
  })

  it("names the failing rule and links to its span", async () => {
    // The link is the point. A policy failure without a span is a claim the reader has to verify
    // by hand, which is step-level attribution degrading back into "something here was wrong".
    stubFetch({
      body: trace({
        evaluations: [
          {
            rule_slug: "outbound-policy",
            rule_kind: "trajectory",
            verdict: "fail",
            score: 0,
            decision_reason: "deterministic",
            error: null,
            detail: {
              failures: [
                {
                  rule_id: "no-send-before-approval",
                  span_id: "child",
                  message: "An email was sent before human approval was received.",
                  severity: "block",
                },
              ],
            },
            created_at: new Date(BASE).toISOString(),
          },
        ],
      }),
    })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() => expect(screen.getByText(/no-send-before-approval/)).toBeTruthy())
    expect(screen.getByText("fail")).toBeTruthy()
    expect(screen.getByText(/An email was sent before human approval/)).toBeTruthy()
    expect(screen.getByRole("button", { name: "child" })).toBeTruthy()
  })

  it("does not present an inconclusive verdict as a failure", async () => {
    // A trace that lost spans cannot answer a question about what did not happen. Colouring that
    // as a violation is how a review queue fills with innocent runs until people stop reading it.
    stubFetch({
      body: trace({
        evaluations: [
          {
            rule_slug: "outbound-policy",
            rule_kind: "trajectory",
            verdict: "inconclusive",
            score: null,
            decision_reason: "deterministic",
            error: null,
            detail: {
              failures: [],
              inconclusive_rules: ["scan-required"],
              incomplete: true,
            },
            created_at: new Date(BASE).toISOString(),
          },
        ],
      }),
    })
    render(<TraceDetailView traceId="abc123" />, { wrapper })

    await waitFor(() => expect(screen.getByText("inconclusive")).toBeTruthy())
    expect(screen.getByText(/scan-required/)).toBeTruthy()
    expect(screen.getByText(/not a violation/)).toBeTruthy()
  })
})
