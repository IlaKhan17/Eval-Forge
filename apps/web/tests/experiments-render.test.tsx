import { ExperimentDetailView } from "@/components/ExperimentDetail"
import { ExperimentList } from "@/components/ExperimentList"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"

/**
 * The experiment views, rendered.
 *
 * What these cover that the pure tests cannot: that the right request is made through the
 * same-origin proxy, that an empty history explains *why* it is empty rather than showing a blank
 * panel, and that absent numbers reach the screen as em dashes. That last one is the property most
 * worth pinning — a 0 where a measurement is missing is a regression someone will go and hunt.
 */

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

/** Route stubbed responses by path, so a component's real request sequence is exercised. */
function stubRoutes(routes: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.replace("/api/ef", "")
      const match = Object.keys(routes).find((key) => path.startsWith(key))
      if (!match) return new Response("null", { status: 404 })
      return new Response(JSON.stringify(routes[match]), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    }),
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const EXPERIMENT = {
  id: "11111111-1111-4111-8111-111111111111",
  name: "reply-intent",
  suite_name: "reply-intent",
  dataset_version_id: "22222222-2222-4222-8222-222222222222",
  dataset_content_hash: "abcdef0123456789",
  git_commit: "0123456789abcdef",
  git_branch: "main",
  is_baseline: false,
}

const RUN = {
  id: "33333333-3333-4333-8333-333333333333",
  experiment_id: EXPERIMENT.id,
  attempt: 1,
  status: "succeeded",
  completed_examples: 40,
  failed_examples: 0,
  total_cost: 0.42,
  started_at: "2026-02-01T10:00:00Z",
  ended_at: "2026-02-01T10:00:30Z",
}

describe("experiment list", () => {
  it("explains an empty history instead of showing a blank panel", async () => {
    // "No experiments" with no explanation reads as a broken page. The cause is almost always that
    // the CI job has no endpoint configured, so the empty state says exactly that.
    stubRoutes({ "/v1/experiments": [] })
    render(<ExperimentList />, { wrapper })

    await waitFor(() => expect(screen.getByText("No experiments yet")).toBeTruthy())
    expect(screen.getByText(/EVALFORGE_ENDPOINT/)).toBeTruthy()
  })

  it("groups runs under their suite and shows the dataset they measured", async () => {
    stubRoutes({ "/v1/experiments": [EXPERIMENT] })
    render(<ExperimentList />, { wrapper })

    await waitFor(() => expect(screen.getByText(/reply-intent/)).toBeTruthy())
    // The hash, not just an id: two runs are only comparable if they measured the same data, and
    // this is the value the gate engine refuses to compare across.
    expect(screen.getByText(/dataset sha abcdef012345/)).toBeTruthy()
  })

  it("says so when an experiment recorded no dataset", async () => {
    stubRoutes({ "/v1/experiments": [{ ...EXPERIMENT, dataset_content_hash: null }] })
    render(<ExperimentList />, { wrapper })

    await waitFor(() => expect(screen.getByText(/nothing to compare against/)).toBeTruthy())
  })
})

describe("experiment detail", () => {
  it("shows a run with its outcome and cost", async () => {
    stubRoutes({
      [`/v1/experiments/${EXPERIMENT.id}/runs`]: [RUN],
      "/v1/experiment-runs": [
        { key: "accuracy", slice: null, value: 0.9, count: 40, error_count: 0 },
      ],
    })
    render(<ExperimentDetailView experimentId={EXPERIMENT.id} />, { wrapper })

    // Waits on the metric rather than the run: the two panels load from separate requests, and
    // asserting on the later one after waiting only for the earlier is a race that passes locally
    // and fails in CI.
    await waitFor(() => expect(screen.getByText("accuracy")).toBeTruthy())
    expect(screen.getByText("succeeded")).toBeTruthy()
    expect(screen.getByText(/40 completed/)).toBeTruthy()
  })

  it("shows an em dash rather than a delta on the first run", async () => {
    // The single most important rendering rule here. A first run has nothing to compare against,
    // and a 0 in the delta column would read as "unchanged" for a metric never measured before.
    stubRoutes({
      [`/v1/experiments/${EXPERIMENT.id}/runs`]: [RUN],
      "/v1/experiment-runs": [
        { key: "accuracy", slice: null, value: 0.9, count: 40, error_count: 0 },
      ],
    })
    render(<ExperimentDetailView experimentId={EXPERIMENT.id} />, { wrapper })

    await waitFor(() => expect(screen.getByText("accuracy")).toBeTruthy())
    expect(screen.getByText(/first run, nothing to compare/)).toBeTruthy()
    // Two: the previous value and the delta, both unknown rather than zero.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2)
  })

  it("reports errored evaluations beside the sample size, not inside it", async () => {
    // A metric measured over 8 of 40 examples is a different claim from one measured over 40, and
    // the mean alone cannot tell you which you are looking at.
    stubRoutes({
      [`/v1/experiments/${EXPERIMENT.id}/runs`]: [RUN],
      "/v1/experiment-runs": [
        { key: "helpfulness", slice: null, value: 0.9, count: 8, error_count: 32 },
      ],
    })
    render(<ExperimentDetailView experimentId={EXPERIMENT.id} />, { wrapper })

    await waitFor(() => expect(screen.getByText("helpfulness")).toBeTruthy())
    expect(screen.getByText(/\+32 err/)).toBeTruthy()
  })

  it("says a run produced no metrics rather than showing an empty table", async () => {
    stubRoutes({ [`/v1/experiments/${EXPERIMENT.id}/runs`]: [RUN], "/v1/experiment-runs": [] })
    render(<ExperimentDetailView experimentId={EXPERIMENT.id} />, { wrapper })

    await waitFor(() => expect(screen.getByText(/produced no metrics/)).toBeTruthy())
  })

  it("surfaces the API's own message when the request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "No such experiment." }), {
            status: 404,
            headers: { "content-type": "application/json" },
          }),
      ),
    )
    render(<ExperimentDetailView experimentId={EXPERIMENT.id} />, { wrapper })

    await waitFor(() => expect(screen.getByText(/No such experiment/)).toBeTruthy())
  })
})
