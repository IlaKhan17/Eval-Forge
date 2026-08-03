import { type NextRequest, NextResponse } from "next/server"

/**
 * Per-request Content-Security-Policy with a nonce.
 *
 * Why the dashboard bothers: the trace viewer renders untrusted content by design —
 * model output, tool arguments, anything an end user of the instrumented application
 * could influence. React's escaping is the primary defence. This is the second, so a
 * single bad component is not an origin compromise.
 *
 * Next.js picks the nonce out of this header and applies it to the scripts it injects,
 * which is what makes a script policy without `'unsafe-inline'` possible at all.
 *
 * `connect-src 'self'` is affordable only because every API call goes through the
 * same-origin proxy in `src/app/api/ef`. Styles still need `'unsafe-inline'`: the
 * framework emits inline style tags and a nonce-based style policy does not survive
 * streaming. Scripts are the half that matters for injection, and those are locked.
 */
export function middleware(request: NextRequest): NextResponse {
  const nonce = crypto.randomUUID().replaceAll("-", "")

  // The dev server compiles with eval, so a policy without `'unsafe-eval'` breaks
  // hot reload. Scoped to development explicitly rather than left permissive
  // everywhere — a relaxation that leaks into production is the usual way this goes
  // wrong.
  const scriptExtras = process.env.NODE_ENV === "production" ? "" : " 'unsafe-eval'"

  const policy = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${scriptExtras}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    ...(process.env.NODE_ENV === "production" ? ["upgrade-insecure-requests"] : []),
  ].join("; ")

  // Forwarded on the request so the server renderer can read it; set on the response
  // so the browser enforces it.
  const headers = new Headers(request.headers)
  headers.set("x-nonce", nonce)
  headers.set("content-security-policy", policy)

  const response = NextResponse.next({ request: { headers } })
  response.headers.set("content-security-policy", policy)
  return response
}

export const config = {
  // Static assets are served with their own headers and do not execute script; running
  // middleware on every one of them costs latency for nothing.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
}
