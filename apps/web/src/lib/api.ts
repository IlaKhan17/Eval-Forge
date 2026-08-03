/**
 * Browser-side API client.
 *
 * Every request goes to this app's own origin at `/api/ef/...`, never directly to the
 * EvalForge API. The proxy on the other side attaches the credential from a
 * server-only environment variable.
 *
 * This is the important decision in the file, so it is worth being explicit about
 * why. The alternative — a `NEXT_PUBLIC_EVALFORGE_API_KEY` read by this module — puts
 * a project-scoped API key in the JavaScript bundle, which means in the page source,
 * in every user's browser cache, and in any CDN that ever served it. There is no way
 * to scope that key tightly enough for it to be safe: read access to traces is read
 * access to captured prompts and tool arguments. Same-origin requests also let the
 * CSP say `connect-src 'self'`, which is a meaningfully stronger policy than one that
 * has to allow an API host.
 *
 * Phase 6 has no login flow, so the proxy uses a single server-side key and the
 * dashboard is single-project. Per-user sessions arrive with the auth phase; the
 * client code here does not change when they do, which is part of the point.
 */

import { type TraceFilters, serializeFilters } from "./filters"
import type { TraceDetail } from "./spans"

export const API_BASE = "/api/ef"

export interface TraceSummary {
  trace_id: string
  name: string
  status: "ok" | "error" | "timeout" | "unset"
  started_at: string
  ended_at: string | null
  duration_ms: number | null
  span_count: number
  error_count: number
  total_tokens: number
  total_cost: number
  dropped_span_count: number
  git_commit: string | null
  metadata: Record<string, unknown>
  tags: Record<string, unknown>
}

export interface TracePage {
  data: TraceSummary[]
  next_cursor: string | null
  has_more: boolean
}

/**
 * A failed request, carrying the problem-details fields the API actually returns.
 *
 * The API speaks RFC 9457 (`application/problem+json`) with a stable `type` and a
 * human-readable `detail`. Surfacing `detail` rather than "Request failed" is the
 * difference between a usable error and a shrug — the server already wrote a good
 * message, so it should not be thrown away here.
 */
export class ApiError extends Error {
  readonly status: number
  readonly type: string
  readonly detail: string
  readonly requestId: string | null

  constructor(init: {
    status: number
    type: string
    detail: string
    title?: string
    requestId?: string | null
  }) {
    super(init.detail || init.title || `HTTP ${init.status}`)
    this.name = "ApiError"
    this.status = init.status
    this.type = init.type
    this.detail = init.detail
    this.requestId = init.requestId ?? null
  }

  /** True when retrying could plausibly succeed. Drives whether a retry is offered. */
  get isTransient(): boolean {
    return this.status >= 500 || this.status === 429
  }
}

interface ProblemDetails {
  type?: unknown
  title?: unknown
  detail?: unknown
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { accept: "application/json", ...init?.headers },
    })
  } catch (cause) {
    // A network failure is not an HTTP status, and treating it as one (status 0)
    // makes every downstream check subtly wrong.
    throw new ApiError({
      status: 0,
      type: "about:network-error",
      detail:
        cause instanceof Error && cause.name === "AbortError"
          ? "The request was cancelled."
          : "Could not reach the EvalForge API. Check that the server is running.",
    })
  }

  if (!response.ok) throw await toApiError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

async function toApiError(response: Response): Promise<ApiError> {
  const requestId = response.headers.get("x-request-id")
  let problem: ProblemDetails = {}
  try {
    problem = (await response.json()) as ProblemDetails
  } catch {
    // A non-JSON body — a proxy error page, an empty 502 — is normal enough that it
    // must not mask the status code with a parse failure.
  }

  return new ApiError({
    status: response.status,
    type: typeof problem.type === "string" ? problem.type : "about:blank",
    detail:
      typeof problem.detail === "string" && problem.detail
        ? problem.detail
        : defaultDetail(response.status),
    title: typeof problem.title === "string" ? problem.title : undefined,
    requestId,
  })
}

function defaultDetail(status: number): string {
  if (status === 401) return "The dashboard is not authenticated against the API."
  if (status === 403) return "This credential is not allowed to read traces."
  // 404 is also what a cross-project read returns, deliberately — see docs/SECURITY.md.
  if (status === 404) return "Not found."
  if (status === 429) return "Rate limited. Try again shortly."
  if (status >= 500) return "The API failed to handle this request."
  return `Request failed with status ${status}.`
}

export function listTraces(filters: TraceFilters, signal?: AbortSignal): Promise<TracePage> {
  const query = serializeFilters(filters)
  return request<TracePage>(`/v1/traces${query ? `?${query}` : ""}`, { signal })
}

export function getTrace(traceId: string, signal?: AbortSignal): Promise<TraceDetail> {
  return request<TraceDetail>(`/v1/traces/${encodeURIComponent(traceId)}`, { signal })
}

/** Readiness, not liveness: the dashboard cares whether the API can serve, not
 * whether its process exists. */
export function getReadiness(signal?: AbortSignal): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("/readyz", { signal })
}
