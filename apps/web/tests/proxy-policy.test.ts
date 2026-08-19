import { checkProxyRequest, filterQuery } from "@/lib/proxy-policy"
import { describe, expect, it } from "vitest"

/**
 * The proxy holds a credential the browser does not have, so everything it forwards, it
 * forwards with authority. These are the tests that keep it from becoming a confused
 * deputy.
 */
describe("checkProxyRequest", () => {
  it("allows the read endpoints the dashboard uses", () => {
    expect(checkProxyRequest("GET", "/v1/traces").allowed).toBe(true)
    expect(checkProxyRequest("GET", "/v1/traces/abc123").allowed).toBe(true)
    expect(checkProxyRequest("GET", "/readyz").allowed).toBe(true)
    expect(checkProxyRequest("HEAD", "/readyz").allowed).toBe(true)
  })

  it("refuses every write method", () => {
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      expect(checkProxyRequest(method, "/v1/traces").allowed).toBe(false)
    }
  })

  it("refuses the write endpoints that would matter most", () => {
    // Reachable through a permissive proxy, `promote-baseline` changes what future gates
    // compare against — a quiet, high-impact write with no business being reachable from
    // a read-only trace viewer. Ingest would let anyone forge traces.
    expect(checkProxyRequest("POST", "/v1/experiments/abc/promote-baseline").allowed).toBe(false)
    expect(checkProxyRequest("POST", "/v1/traces:ingest").allowed).toBe(false)
    // And not even as a GET, since the allow-list is by path as well as by method.
    expect(checkProxyRequest("GET", "/v1/experiments/abc/promote-baseline").allowed).toBe(false)
  })

  it("refuses an unlisted read endpoint", () => {
    expect(checkProxyRequest("GET", "/v1/api-keys").allowed).toBe(false)
    expect(checkProxyRequest("GET", "/openapi.json").allowed).toBe(false)
  })

  it("refuses traversal and unnormalized paths", () => {
    // Without this, one allow-listed endpoint becomes all of them.
    expect(checkProxyRequest("GET", "/v1/traces/../api-keys").allowed).toBe(false)
    expect(checkProxyRequest("GET", "/v1/traces//abc").allowed).toBe(false)
    expect(checkProxyRequest("GET", "/v1/traces/abc\\..\\keys").allowed).toBe(false)
    expect(checkProxyRequest("GET", "v1/traces").allowed).toBe(false)
  })

  it("refuses a trace id containing a path separator", () => {
    expect(checkProxyRequest("GET", "/v1/traces/abc/spans").allowed).toBe(false)
  })

  it("refuses an absurdly long trace id", () => {
    expect(checkProxyRequest("GET", `/v1/traces/${"a".repeat(300)}`).allowed).toBe(false)
  })

  it("explains why it refused", () => {
    // The reason ends up in the 403 body; "forbidden" with no detail is what makes a
    // proxy infuriating to work with.
    const decision = checkProxyRequest("POST", "/v1/traces")
    expect(decision.reason).toContain("changed through the CLI")
  })
})

describe("filterQuery", () => {
  it("passes the documented trace filters through", () => {
    const filtered = filterQuery(new URLSearchParams("status=error&limit=10&cursor=abc"))
    expect(filtered.get("status")).toBe("error")
    expect(filtered.get("limit")).toBe("10")
    expect(filtered.get("cursor")).toBe("abc")
  })

  it("drops anything unrecognized", () => {
    // So a future API parameter is not reachable through the dashboard before anyone has
    // decided whether it should be.
    const filtered = filterQuery(new URLSearchParams("status=ok&project_id=other&include=secrets"))
    expect(filtered.has("project_id")).toBe(false)
    expect(filtered.has("include")).toBe(false)
    expect(filtered.get("status")).toBe("ok")
  })

  it("preserves repeated allowed keys", () => {
    const filtered = filterQuery(new URLSearchParams("status=ok&status=error"))
    expect(filtered.getAll("status")).toEqual(["ok", "error"])
  })
})

describe("session-carrying proxy", () => {
  /**
   * The proxy now forwards the caller's own session rather than a shared server-side key, so a
   * write is no longer a confused deputy. What the allow-list still does is keep the browser's
   * reachable surface small and deliberate.
   */

  it("allows the account writes the dashboard actually makes", () => {
    const org = "11111111-1111-4111-8111-111111111111"
    const project = "22222222-2222-4222-8222-222222222222"
    const member = "33333333-3333-4333-8333-333333333333"

    expect(checkProxyRequest("POST", `/v1/orgs/${org}/invites`).allowed).toBe(true)
    expect(checkProxyRequest("PATCH", `/v1/orgs/${org}/members/${member}`).allowed).toBe(true)
    expect(checkProxyRequest("POST", `/v1/projects/${project}/api-keys`).allowed).toBe(true)
    expect(checkProxyRequest("PUT", "/v1/ops/budget").allowed).toBe(true)
  })

  it("still refuses the writes that change what a gate compares against", () => {
    // Promoting a baseline changes the answer for every future run of a suite. It is a decision
    // that belongs in a repository and a review, not behind a button on a dashboard.
    const experiment = "11111111-1111-4111-8111-111111111111"
    expect(
      checkProxyRequest("POST", `/v1/experiments/${experiment}/promote-baseline`).allowed,
    ).toBe(false)
    expect(checkProxyRequest("POST", "/v1/datasets").allowed).toBe(false)
    expect(checkProxyRequest("POST", "/v1/quality-gate-sets").allowed).toBe(false)
  })

  it("does not let a read pattern authorise a write to the same path", () => {
    // The two lists are separate on purpose: "the dashboard can read X" must never silently mean
    // "the dashboard can write X".
    const org = "11111111-1111-4111-8111-111111111111"
    expect(checkProxyRequest("GET", `/v1/orgs/${org}/members`).allowed).toBe(true)
    expect(checkProxyRequest("POST", `/v1/orgs/${org}/members`).allowed).toBe(false)
  })

  it("refuses a write to a path shaped like an allowed one", () => {
    // The id patterns are anchored, so a traversal or a suffix cannot ride in on a match.
    expect(checkProxyRequest("POST", "/v1/orgs/not-a-uuid/invites").allowed).toBe(false)
    expect(
      checkProxyRequest("POST", "/v1/orgs/11111111-1111-4111-8111-111111111111/invites/extra")
        .allowed,
    ).toBe(false)
  })
})

describe("requests that must work without a session", () => {
  it("marks the invitation preview as anonymous", () => {
    // The people this endpoint exists for are precisely the ones with no account. Requiring a
    // session here means an invitation can only be read by someone who does not need it — which is
    // what shipped, because the unit tests stub `fetch` and never reach the proxy handler.
    expect(checkProxyRequest("GET", "/v1/invites/preview").anonymous).toBe(true)
  })

  it("marks everything else as needing one", () => {
    // The default is what keeps this exception narrow. A new endpoint is authenticated unless
    // somebody adds it to the list on purpose.
    expect(checkProxyRequest("GET", "/v1/traces").anonymous).toBe(false)
    expect(checkProxyRequest("GET", "/v1/auth/me").anonymous).toBe(false)
    expect(checkProxyRequest("GET", "/v1/orgs").anonymous).toBe(false)
  })

  it("does not make a write anonymous just because a read is", () => {
    // Accepting an invitation genuinely requires a session: the API matches the signed-in address
    // against the one invited, which is what stops a forwarded link becoming a transferable
    // membership.
    expect(checkProxyRequest("POST", "/v1/invites/accept").anonymous).toBeFalsy()
  })
})
