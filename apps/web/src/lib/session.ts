import "server-only"

/**
 * The signed-in session, held in cookies the browser cannot read.
 *
 * This replaces the dashboard's original model, where the proxy carried one server-side API key and
 * every visitor got whatever that key could do. That was defensible when the dashboard was assumed
 * to sit behind someone else's SSO; it is not a product. Now the proxy carries *the caller's own*
 * session, so what the dashboard can reach is exactly what that person can reach.
 *
 * Three decisions hold the security of this file together:
 *
 * **httpOnly cookies, not localStorage.** A token in `localStorage` is readable by any script that
 * ends up on the page — one bad dependency, one injected analytics snippet — and exfiltrating it is
 * a single line. An httpOnly cookie is not reachable from JavaScript at all, so the same XSS gets
 * to *use* the session while the tab is open but cannot steal it for later.
 *
 * **`SameSite=Lax` plus a required custom header on writes.** Lax alone stops cross-site form posts
 * carrying the cookie; the header stops everything else, because a cross-origin page cannot set a
 * custom header without a CORS preflight the API will refuse. Either would probably do. Together
 * they mean a CSRF bug needs two mistakes rather than one.
 *
 * **Refresh happens server-side, once, transparently.** The access token is short-lived by design.
 * If the client had to notice a 401 and re-request, every component would need that logic and one
 * of them would forget; doing it in the proxy means a expiring session is invisible to the UI.
 */

import { cookies } from "next/headers"

const ACCESS_COOKIE = "ps_access"
const REFRESH_COOKIE = "ps_refresh"

/** Marks a request as same-origin. See the note on CSRF above. */
export const REQUEST_HEADER = "x-proofstep-request"

export interface SessionTokens {
  accessToken: string
  refreshToken: string
}

function cookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    // Only over TLS in production. Left off in development because localhost is http and a
    // `Secure` cookie there is silently dropped — which looks exactly like a broken login.
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax" as const,
    path: "/",
    maxAge,
  }
}

export async function readSession(): Promise<Partial<SessionTokens>> {
  const jar = await cookies()
  return {
    accessToken: jar.get(ACCESS_COOKIE)?.value,
    refreshToken: jar.get(REFRESH_COOKIE)?.value,
  }
}

export async function writeSession(tokens: SessionTokens): Promise<void> {
  const jar = await cookies()
  // The access cookie outlives the token itself on purpose: the proxy detects expiry from the
  // API's 401 and refreshes, which is more reliable than trusting the browser's clock.
  jar.set(ACCESS_COOKIE, tokens.accessToken, cookieOptions(60 * 60))
  jar.set(REFRESH_COOKIE, tokens.refreshToken, cookieOptions(60 * 60 * 24 * 30))
}

export async function clearSession(): Promise<void> {
  const jar = await cookies()
  jar.set(ACCESS_COOKIE, "", cookieOptions(0))
  jar.set(REFRESH_COOKIE, "", cookieOptions(0))
}
