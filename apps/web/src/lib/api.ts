/**
 * Browser-side API client.
 *
 * Every request goes to this app's own origin at `/api/ps/...`, never directly to the
 * Proofstep API. The proxy on the other side attaches the credential from a
 * server-only environment variable.
 *
 * This is the important decision in the file, so it is worth being explicit about
 * why. The alternative — a `NEXT_PUBLIC_PROOFSTEP_API_KEY` read by this module — puts
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

import type { Experiment, Metric, Run } from "./experiments"
import { type TraceFilters, serializeFilters } from "./filters"
import type { TraceDetail } from "./spans"

export const API_BASE = "/api/ps"

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
          : "Could not reach the Proofstep API. Check that the server is running.",
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

export function listExperiments(suiteName?: string, signal?: AbortSignal): Promise<Experiment[]> {
  const query = suiteName ? `?suite_name=${encodeURIComponent(suiteName)}` : ""
  return request<Experiment[]>(`/v1/experiments${query}`, { signal })
}

export function listRuns(experimentId: string, signal?: AbortSignal): Promise<Run[]> {
  return request<Run[]>(`/v1/experiments/${encodeURIComponent(experimentId)}/runs`, { signal })
}

export function getRunMetrics(runId: string, signal?: AbortSignal): Promise<Metric[]> {
  return request<Metric[]>(`/v1/experiment-runs/${encodeURIComponent(runId)}/metrics`, { signal })
}

/**
 * A same-origin write.
 *
 * The header is what distinguishes this from a cross-site request: a page on another origin cannot
 * set it without a CORS preflight the API refuses. `SameSite=Lax` on the session cookie already
 * stops the common case; this covers the rest. See `lib/session.ts`.
 */
const SAME_ORIGIN = { "x-proofstep-request": "1", "content-type": "application/json" }

function write<T>(path: string, method: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: SAME_ORIGIN,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export interface Me {
  id: string
  email: string
  name: string | null
  organizations: { org_id: string; org_name: string; org_slug: string; role: string }[]
}

export interface Member {
  user_id: string
  email: string
  name: string | null
  role: string
  joined_at: string
}

export interface ApiKeySummary {
  id: string
  name: string
  prefix: string
  scopes: string[]
  created_at: string
  last_used_at: string | null
  expires_at: string | null
  revoked_at: string | null
  /** Present only in the response that created it. */
  token?: string | null
}

export interface ProjectSummary {
  id: string
  org_id: string
  name: string
  slug: string
}

export function getMe(signal?: AbortSignal): Promise<Me> {
  return request<Me>("/v1/auth/me", { signal })
}

export function listProjects(orgId: string, signal?: AbortSignal): Promise<ProjectSummary[]> {
  return request<ProjectSummary[]>(`/v1/orgs/${orgId}/projects`, { signal })
}

export function createProject(orgId: string, name: string): Promise<ProjectSummary> {
  return write<ProjectSummary>(`/v1/orgs/${orgId}/projects`, "POST", { name })
}

export function listMembers(orgId: string, signal?: AbortSignal): Promise<Member[]> {
  return request<Member[]>(`/v1/orgs/${orgId}/members`, { signal })
}

export function inviteMember(
  orgId: string,
  email: string,
  role: string,
): Promise<{ id: string; email: string; role: string; token: string | null }> {
  return write(`/v1/orgs/${orgId}/invites`, "POST", { email, role })
}

export function changeRole(orgId: string, userId: string, role: string): Promise<Member> {
  return write<Member>(`/v1/orgs/${orgId}/members/${userId}`, "PATCH", { role })
}

export function removeMember(orgId: string, userId: string): Promise<void> {
  return write<void>(`/v1/orgs/${orgId}/members/${userId}`, "DELETE")
}

export function listApiKeys(projectId: string, signal?: AbortSignal): Promise<ApiKeySummary[]> {
  return request<ApiKeySummary[]>(`/v1/projects/${projectId}/api-keys`, { signal })
}

export function createApiKey(
  projectId: string,
  body: { name: string; scopes: string[] },
): Promise<ApiKeySummary> {
  return write<ApiKeySummary>(`/v1/projects/${projectId}/api-keys`, "POST", body)
}

export function revokeApiKey(projectId: string, keyId: string): Promise<void> {
  return write<void>(`/v1/projects/${projectId}/api-keys/${keyId}`, "DELETE")
}

/** Sign-in and sign-out go to the dashboard's own origin, never to the API directly. */
export async function signIn(
  action: "login" | "signup",
  body: Record<string, unknown>,
): Promise<{ org_id: string | null; project_id: string | null }> {
  const response = await fetch(`/api/auth/${action}`, {
    method: "POST",
    headers: SAME_ORIGIN,
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await toApiError(response)
  return (await response.json()) as { org_id: string | null; project_id: string | null }
}

export async function signOut(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST", headers: SAME_ORIGIN })
}
