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
    expect(decision.reason).toContain("read requests only")
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
