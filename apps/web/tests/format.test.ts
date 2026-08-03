import {
  formatCost,
  formatDelta,
  formatDuration,
  formatRelative,
  formatScore,
  formatTokens,
  shortId,
} from "@/lib/format"
import { describe, expect, it } from "vitest"

describe("formatDuration", () => {
  it("scales the unit to the magnitude", () => {
    expect(formatDuration(0.4)).toBe("<1ms")
    expect(formatDuration(250)).toBe("250ms")
    expect(formatDuration(1_500)).toBe("1.50s")
    expect(formatDuration(125_000)).toBe("2m 5s")
  })

  it("shows a missing duration as unknown, not as zero", () => {
    // An unfinished span has no duration. Rendering it as 0ms would claim it was instant.
    expect(formatDuration(null)).toBe("—")
    expect(formatDuration(undefined)).toBe("—")
    expect(formatDuration(Number.NaN)).toBe("—")
  })
})

describe("formatCost", () => {
  it("keeps sub-cent costs legible", () => {
    // Two decimals would make every span $0.00 and the column useless.
    expect(formatCost(0.000_042)).toBe("$0.000042")
    expect(formatCost(0.25)).toBe("$0.2500")
    expect(formatCost(12.5)).toBe("$12.50")
  })

  it("distinguishes free from not measured", () => {
    // One means the span cost nothing; the other means nobody instrumented it. Showing
    // both as $0.00 is how someone concludes their agent is cheap.
    expect(formatCost(0)).toBe("$0")
    expect(formatCost(null)).toBe("—")
  })
})

describe("formatTokens", () => {
  it("abbreviates large counts", () => {
    expect(formatTokens(999)).toBe("999")
    expect(formatTokens(1_500)).toBe("1.5k")
    expect(formatTokens(2_500_000)).toBe("2.50M")
  })
})

describe("formatScore and formatDelta", () => {
  it("uses a fixed width so a column can be scanned", () => {
    expect(formatScore(0.5)).toBe("0.500")
    expect(formatScore(1)).toBe("1.000")
  })

  it("signs a delta and shows zero unsigned", () => {
    expect(formatDelta(0.012_4)).toBe("+0.012")
    expect(formatDelta(-0.5)).toBe("-0.500")
    expect(formatDelta(0)).toBe("0.000")
    expect(formatDelta(null)).toBe("—")
  })
})

describe("formatRelative", () => {
  const now = Date.parse("2026-01-02T00:00:00Z")

  it("scales the unit", () => {
    expect(formatRelative("2026-01-01T23:59:30Z", now)).toBe("30s ago")
    expect(formatRelative("2026-01-01T23:30:00Z", now)).toBe("30m ago")
    expect(formatRelative("2026-01-01T12:00:00Z", now)).toBe("12h ago")
    expect(formatRelative("2025-12-30T00:00:00Z", now)).toBe("3d ago")
  })

  it("does not report a clock-skewed future as a negative age", () => {
    // Ingest clocks are the application's, not the server's, so a trace can arrive
    // stamped slightly in the future. "-3s ago" reads as a bug in the dashboard.
    expect(formatRelative("2026-01-02T00:00:03Z", now)).toBe("just now")
  })

  it("falls back to an absolute timestamp beyond a month", () => {
    expect(formatRelative("2025-01-01T00:00:00Z", now)).not.toContain("ago")
  })

  it("handles an unparseable value", () => {
    expect(formatRelative("not a date", now)).toBe("—")
    expect(formatRelative(null, now)).toBe("—")
  })
})

describe("shortId", () => {
  it("shortens only when there is something to shorten", () => {
    expect(shortId("0123456789abcdef")).toBe("01234567")
    expect(shortId("abc")).toBe("abc")
    expect(shortId("0123456789abcdef", 7)).toBe("0123456")
  })
})
