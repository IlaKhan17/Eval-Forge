import {
  DEFAULT_LIMIT,
  isFiltered,
  parseFilters,
  queryKey,
  serializeFilters,
  withFilter,
} from "@/lib/filters"
import { describe, expect, it } from "vitest"

const parse = (query: string) => parseFilters(new URLSearchParams(query))

describe("parseFilters", () => {
  it("defaults the limit", () => {
    expect(parse("")).toEqual({ limit: DEFAULT_LIMIT })
  })

  it("reads the supported filters", () => {
    const filters = parse("name=reply&status=error&has_errors=true&min_duration_ms=10")
    expect(filters).toMatchObject({
      name: "reply",
      status: "error",
      has_errors: true,
      min_duration_ms: 10,
    })
  })

  it("drops an unknown status rather than failing", () => {
    // A stale link should still show a trace list. Dropping the filter widens the
    // result set, so nothing is hidden by being lenient.
    expect(parse("status=weird").status).toBeUndefined()
  })

  it("drops a non-commit git_commit", () => {
    // Passing arbitrary text through would match no rows and render as "no traces yet",
    // which reads as a data problem rather than a bad filter.
    expect(parse("git_commit=main").git_commit).toBeUndefined()
    expect(parse("git_commit=A1B2C3D").git_commit).toBe("a1b2c3d")
  })

  it("caps the limit and rejects a nonsense one", () => {
    expect(parse("limit=99999").limit).toBe(200)
    expect(parse("limit=0").limit).toBe(DEFAULT_LIMIT)
    expect(parse("limit=abc").limit).toBe(DEFAULT_LIMIT)
    expect(parse("limit=1.5").limit).toBe(DEFAULT_LIMIT)
  })

  it("resolves a relative time shorthand", () => {
    const since = parse("since=-24h").since
    expect(since).toBeDefined()
    const ageHours = (Date.now() - Date.parse(since as string)) / 3_600_000
    expect(ageHours).toBeCloseTo(24, 1)
  })

  it("normalizes an absolute timestamp to ISO", () => {
    expect(parse("since=2026-01-01T00:00:00Z").since).toBe("2026-01-01T00:00:00.000Z")
  })

  it("drops the narrower bound of an inverted duration range", () => {
    // min > max matches nothing, and an empty list is indistinguishable from no data.
    const filters = parse("min_duration_ms=500&max_duration_ms=100")
    expect(filters.min_duration_ms).toBe(500)
    expect(filters.max_duration_ms).toBeUndefined()
  })

  it("drops an inverted time range the same way", () => {
    const filters = parse("since=2026-02-01T00:00:00Z&until=2026-01-01T00:00:00Z")
    expect(filters.since).toBeDefined()
    expect(filters.until).toBeUndefined()
  })

  it("distinguishes has_errors=false from absent", () => {
    // `false` is a real filter — "show me only clean traces" — not the default.
    expect(parse("has_errors=false").has_errors).toBe(false)
    expect(parse("").has_errors).toBeUndefined()
  })
})

describe("serializeFilters", () => {
  it("round-trips", () => {
    const filters = parse("name=reply&status=error&has_errors=true&limit=25")
    expect(parse(serializeFilters(filters))).toEqual(filters)
  })

  it("omits the default limit so a plain URL stays clean", () => {
    expect(serializeFilters({ limit: DEFAULT_LIMIT })).toBe("")
  })

  it("emits keys in a stable order regardless of insertion order", () => {
    // Otherwise React Query caches one page under several keys and the history fills
    // with entries that differ only in parameter order.
    const a = serializeFilters({ limit: DEFAULT_LIMIT, status: "ok", name: "x" })
    const b = serializeFilters({ name: "x", status: "ok", limit: DEFAULT_LIMIT })
    expect(a).toBe(b)
    expect(queryKey(parse(a))).toEqual(queryKey(parse(b)))
  })
})

describe("withFilter", () => {
  it("drops the cursor when any other filter changes", () => {
    // A cursor is an anchor into a specific result set. Carried across a filter change,
    // it would silently start the list partway into a set it never belonged to.
    const next = withFilter({ limit: DEFAULT_LIMIT, cursor: "abc" }, "status", "error")
    expect(next.cursor).toBeUndefined()
    expect(next.status).toBe("error")
  })

  it("keeps the cursor when paginating", () => {
    const next = withFilter({ limit: DEFAULT_LIMIT, status: "error" }, "cursor", "abc")
    expect(next).toEqual({ limit: DEFAULT_LIMIT, status: "error", cursor: "abc" })
  })

  it("removes a filter set to undefined", () => {
    const next = withFilter({ limit: DEFAULT_LIMIT, name: "x" }, "name", undefined)
    expect("name" in next).toBe(false)
  })
})

describe("isFiltered", () => {
  it("ignores the limit and the cursor", () => {
    // Neither narrows the result set, so neither should make the UI offer "clear
    // filters" or change the empty-state message.
    expect(isFiltered({ limit: 200, cursor: "abc" })).toBe(false)
    expect(isFiltered({ limit: DEFAULT_LIMIT, status: "ok" })).toBe(true)
    expect(isFiltered({ limit: DEFAULT_LIMIT, has_errors: false })).toBe(true)
  })
})
