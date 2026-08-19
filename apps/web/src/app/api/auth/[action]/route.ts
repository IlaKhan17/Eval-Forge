/**
 * Sign-in, sign-up, and sign-out, on the dashboard's own origin.
 *
 * These exist so the browser never holds a token. The page posts credentials here, this handler
 * talks to the API, and the resulting session lands in httpOnly cookies the page cannot read —
 * see `lib/session.ts` for why that boundary is where it is.
 *
 * Deliberately *not* a general proxy: only three actions, each explicitly named. A handler that
 * forwarded any `/v1/auth/*` path would also forward `refresh`, and a refresh reachable from a page
 * is a token-rotation endpoint an attacker can drive.
 */

import { ConfigError, serverConfig } from "@/lib/server-config"
import { REQUEST_HEADER, clearSession, readSession, writeSession } from "@/lib/session"
import { NextResponse } from "next/server"

const ACTIONS = new Set(["login", "signup", "logout", "forgot", "reset"])

/**
 * Actions that authenticate. Everything else here is a request *about* an account rather than a
 * way into one, and must not be allowed to write a session cookie.
 *
 * This distinction is the reason the set exists. `forgot` and `reset` return a bland
 * acknowledgement with no tokens in it, so the session-writing branch below would store two
 * `undefined`s — quietly replacing whatever session the browser already had with a broken one.
 * Signing out the person who just reset their password would at least be visible; signing out a
 * bystander who mistyped an address on the forgot form would not be.
 */
const AUTHENTICATING = new Set(["login", "signup"])

function problem(status: number, detail: string, title = "Request failed"): NextResponse {
  return NextResponse.json(
    { type: "about:blank", title, status, detail },
    { status, headers: { "content-type": "application/problem+json" } },
  )
}

export async function POST(
  request: Request,
  context: { params: Promise<{ action: string }> },
): Promise<NextResponse> {
  const { action } = await context.params
  if (!ACTIONS.has(action)) return problem(404, `Unknown action: ${action}.`)

  // Same CSRF guard as the API proxy: a cross-origin page cannot set this header without a
  // preflight, and login is a state-changing request like any other.
  if (request.headers.get(REQUEST_HEADER) !== "1") {
    return problem(403, "This endpoint requires a same-origin request.", "Forbidden")
  }

  let apiUrl: string
  try {
    apiUrl = serverConfig().apiUrl
  } catch (error) {
    if (error instanceof ConfigError) return problem(503, error.message, "Not configured")
    throw error
  }

  if (action === "logout") {
    const { refreshToken } = await readSession()
    if (refreshToken) {
      // Best effort. The cookies are cleared either way — a sign-out that leaves the browser
      // holding a session because the API was briefly unreachable is the wrong failure.
      await fetch(`${apiUrl}/v1/auth/logout`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
        cache: "no-store",
      }).catch(() => undefined)
    }
    await clearSession()
    return NextResponse.json({ ok: true })
  }

  const body = await request.text()
  const upstream = await fetch(`${apiUrl}/v1/auth/${action}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body,
    cache: "no-store",
  }).catch(() => null)

  if (upstream === null) {
    return problem(502, "Could not reach the Proofstep API.", "Upstream unreachable")
  }

  const payload = await upstream.json().catch(() => ({}))
  if (!upstream.ok) {
    // The API's own message, passed through. It already distinguishes "that email is taken" from
    // "those credentials do not match" with the care that distinction deserves.
    return NextResponse.json(payload, {
      status: upstream.status,
      headers: { "content-type": "application/problem+json" },
    })
  }

  if (!AUTHENTICATING.has(action)) {
    // Passed through as-is: the API's acknowledgement is deliberately the same whether or not the
    // address exists, and the page shows exactly what it says.
    return NextResponse.json(payload)
  }

  await writeSession({
    accessToken: payload.access_token,
    refreshToken: payload.refresh_token,
  })

  // The tokens themselves are not echoed to the page. Everything the UI needs to route the user is.
  return NextResponse.json({
    user_id: payload.user_id,
    org_id: payload.org_id,
    project_id: payload.project_id,
  })
}
