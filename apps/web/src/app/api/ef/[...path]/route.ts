/**
 * Proxy to the Proofstep API, carrying the caller's own session.
 *
 * Originally this held one server-side API key and forwarded reads only — a sensible shape when the
 * dashboard was assumed to sit behind someone else's SSO, and the wrong shape for a product: every
 * visitor got whatever that key could do. It now forwards the signed-in user's session from an
 * httpOnly cookie, so the dashboard's authority is exactly the caller's authority.
 *
 * That change makes writes safe to forward (a user acting as themselves is not a confused deputy)
 * and makes CSRF the thing to guard instead — hence the required same-origin header. See
 * `lib/session.ts` for the cookie reasoning and `lib/proxy-policy.ts` for what may pass.
 *
 * A 401 from upstream triggers one transparent refresh-and-retry. Access tokens are short-lived by
 * design; without this every component in the app would need to notice expiry and one of them would
 * forget.
 */

import { checkProxyRequest, filterQuery } from "@/lib/proxy-policy"
import { ConfigError, serverConfig } from "@/lib/server-config"
import { REQUEST_HEADER, clearSession, readSession, writeSession } from "@/lib/session"
import { NextResponse } from "next/server"

/** Fail fast rather than holding a connection open behind a spinner forever. */
const UPSTREAM_TIMEOUT_MS = 30_000

function problem(status: number, type: string, title: string, detail: string): NextResponse {
  // Same problem+json shape the API uses, so the client's error handling does not
  // need a second code path for proxy-originated failures.
  return NextResponse.json(
    { type, title, status, detail },
    { status, headers: { "content-type": "application/problem+json" } },
  )
}

async function handle(request: Request, path: string[]): Promise<NextResponse> {
  const target = `/${path.join("/")}`
  const decision = checkProxyRequest(request.method, target)
  if (!decision.allowed) {
    return problem(
      403,
      "https://proofstep.dev/problems/proxy-forbidden",
      "Not proxied",
      decision.reason ?? "Not allowed.",
    )
  }

  // Writes must be same-origin. `SameSite=Lax` already blocks a cross-site form post from carrying
  // the cookie; this stops the rest, because a cross-origin script cannot set a custom header
  // without a preflight the API refuses.
  if (request.method !== "GET" && request.method !== "HEAD") {
    if (request.headers.get(REQUEST_HEADER) !== "1") {
      return problem(
        403,
        "https://proofstep.dev/problems/proxy-forbidden",
        "Not proxied",
        "A write through the dashboard proxy must be a same-origin request.",
      )
    }
  }

  let config: { apiUrl: string; apiKey?: string }
  try {
    config = serverConfig()
  } catch (error) {
    if (error instanceof ConfigError) {
      // 503, not 500: the dashboard is not misbehaving, it is not configured. The
      // detail text is the actual fix, so it is passed through verbatim.
      return problem(
        503,
        "https://proofstep.dev/problems/dashboard-not-configured",
        "Dashboard is not configured",
        error.message,
      )
    }
    throw error
  }

  const { accessToken, refreshToken } = await readSession()
  if (!accessToken) {
    return problem(
      401,
      "https://proofstep.dev/problems/not-signed-in",
      "Not signed in",
      "Sign in to view this.",
    )
  }

  const incoming = new URL(request.url)
  const query = filterQuery(incoming.searchParams).toString()
  const upstream = `${config.apiUrl}${target}${query ? `?${query}` : ""}`

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS)
  // Also abort when the browser goes away, so a closed tab does not leave the
  // upstream request running.
  request.signal.addEventListener("abort", () => controller.abort())

  // Read once: the body has to survive a retry after a refresh, and a stream can only be consumed
  // one time.
  const payload =
    request.method === "GET" || request.method === "HEAD" ? undefined : await request.text()

  try {
    const send = (token: string) =>
      fetch(upstream, {
        method: request.method,
        headers: {
          authorization: `Bearer ${token}`,
          accept: "application/json",
          ...(payload === undefined ? {} : { "content-type": "application/json" }),
        },
        body: payload,
        signal: controller.signal,
        // Never cache: a trace list is live data, and a cached authenticated response
        // is exactly the kind of thing that leaks between projects later on.
        cache: "no-store",
        redirect: "manual",
      })

    let response = await send(accessToken)

    if (response.status === 401 && refreshToken) {
      const rotated = await refreshSession(config.apiUrl, refreshToken)
      if (rotated) {
        await writeSession(rotated)
        response = await send(rotated.accessToken)
      } else {
        // The refresh token is dead too — expired, or revoked because someone replayed it. Clearing
        // the cookies is what turns the next page load into a login screen rather than a loop.
        await clearSession()
      }
    }

    const body = await response.text()
    const headers = new Headers({
      "content-type": response.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    })
    // Useful for correlating a dashboard error with a server log; harmless to expose.
    const requestId = response.headers.get("x-request-id")
    if (requestId) headers.set("x-request-id", requestId)

    return new NextResponse(body, { status: response.status, headers })
  } catch (error) {
    if (controller.signal.aborted) {
      return problem(
        504,
        "https://proofstep.dev/problems/upstream-timeout",
        "Upstream timed out",
        `The API did not respond within ${UPSTREAM_TIMEOUT_MS / 1000}s.`,
      )
    }
    return problem(
      502,
      "https://proofstep.dev/problems/upstream-unreachable",
      "Upstream unreachable",
      // The upstream URL is not echoed: it may contain an internal hostname, and the
      // browser has no use for it.
      error instanceof Error ? error.message : "Could not reach the Proofstep API.",
    )
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Exchange a refresh token for a new pair, or `null` if it is no longer valid.
 *
 * Failure is not an error here. A refresh token expires, and it is revoked outright when the API
 * detects a replay — both are ordinary reasons to send someone back to the login page.
 */
async function refreshSession(
  apiUrl: string,
  refreshToken: string,
): Promise<{ accessToken: string; refreshToken: string } | null> {
  const response = await fetch(`${apiUrl}/v1/auth/refresh`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
    cache: "no-store",
  }).catch(() => null)

  if (!response?.ok) return null
  const payload = await response.json().catch(() => null)
  if (!payload?.access_token || !payload?.refresh_token) return null
  return { accessToken: payload.access_token, refreshToken: payload.refresh_token }
}

export async function GET(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const { path } = await context.params
  return handle(request, path)
}

export async function HEAD(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const { path } = await context.params
  return handle(request, path)
}

export async function POST(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const { path } = await context.params
  return handle(request, path)
}

export async function PATCH(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const { path } = await context.params
  return handle(request, path)
}

export async function PUT(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const { path } = await context.params
  return handle(request, path)
}

export async function DELETE(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const { path } = await context.params
  return handle(request, path)
}
