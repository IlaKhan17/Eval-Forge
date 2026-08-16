/**
 * Safe rendering of captured payloads.
 *
 * Span inputs and outputs are arbitrary values from someone else's application, and
 * an LLM output is arbitrary text an *end user* may have influenced. So the payload
 * viewer is the one place in this dashboard where untrusted content is displayed, and
 * it gets treated as untrusted.
 *
 * Three concerns, in order of how badly they bite:
 *
 * 1. **Injection.** React escapes text children, so the defence is simply never to
 *    reach for `dangerouslySetInnerHTML` — enforced by a Biome rule rather than by
 *    remembering. This module never returns markup, only strings and tokens.
 * 2. **Denial of service by size.** A 50 MB tool result stringified into the DOM
 *    freezes the tab. Truncation is applied here and always *labelled*, because
 *    silently-shortened output is how someone debugs the wrong thing for an hour.
 * 3. **Leaked secrets.** The SDK redacts before export, but a payload that predates
 *    a redaction rule, or arrived through the raw ingest API, can still carry one.
 *    A second pass at display time is cheap.
 */

/** Beyond this the viewer shows a truncated body and offers a download. */
export const MAX_RENDER_CHARS = 200_000
/** Nesting past this is collapsed; deeper structures are rarely read as a tree. */
export const MAX_DEPTH = 12
/** Arrays longer than this are elided in the middle. */
export const MAX_ARRAY_ITEMS = 200

export interface RenderedPayload {
  text: string
  truncated: boolean
  /** Characters dropped, so the UI can say how much is missing. */
  omittedChars: number
  /** True when a value could not be represented (a cycle, a BigInt, a function). */
  lossy: boolean
}

/**
 * Stringify a payload for display.
 *
 * Cycle-safe and depth-limited. `JSON.stringify` throws on a cycle, and a throw here
 * would blank the whole inspector over one bad span — the payload viewer must degrade,
 * never disappear.
 */
export function renderPayload(value: unknown): RenderedPayload {
  if (value === undefined) {
    return { text: "", truncated: false, omittedChars: 0, lossy: false }
  }

  let lossy = false
  const seen = new WeakSet<object>()

  const prepare = (input: unknown, depth: number): unknown => {
    if (input === null) return null

    const kind = typeof input
    if (kind === "string" || kind === "number" || kind === "boolean") return input
    if (kind === "bigint") {
      lossy = true
      return `${(input as bigint).toString()}n`
    }
    if (kind === "function" || kind === "symbol") {
      lossy = true
      return `[${kind}]`
    }
    if (kind !== "object") return String(input)

    const object = input as object
    if (seen.has(object)) {
      lossy = true
      return "[circular]"
    }
    if (depth >= MAX_DEPTH) {
      lossy = true
      return Array.isArray(object) ? "[…nested array]" : "[…nested object]"
    }

    seen.add(object)
    try {
      if (Array.isArray(object)) {
        if (object.length > MAX_ARRAY_ITEMS) {
          lossy = true
          const head = object.slice(0, MAX_ARRAY_ITEMS).map((item) => prepare(item, depth + 1))
          return [...head, `[…${object.length - MAX_ARRAY_ITEMS} more items]`]
        }
        return object.map((item) => prepare(item, depth + 1))
      }
      const out: Record<string, unknown> = {}
      for (const [key, item] of Object.entries(object)) {
        out[key] = prepare(item, depth + 1)
      }
      return out
    } finally {
      // Removed on the way out so a value that legitimately appears twice in
      // different branches is not mislabelled as circular.
      seen.delete(object)
    }
  }

  let text: string
  try {
    text = JSON.stringify(prepare(value, 0), null, 2) ?? String(value)
  } catch {
    lossy = true
    text = String(value)
  }

  if (text.length > MAX_RENDER_CHARS) {
    return {
      text: text.slice(0, MAX_RENDER_CHARS),
      truncated: true,
      omittedChars: text.length - MAX_RENDER_CHARS,
      lossy,
    }
  }
  return { text, truncated: false, omittedChars: 0, lossy }
}

/**
 * Whether a URL is safe to put in an `href`.
 *
 * Allow-list, not deny-list. `javascript:` is the obvious case, but `data:` and
 * `vbscript:` are equally live, and a deny-list has to be right about every scheme
 * that will ever exist.
 */
export function isSafeHref(href: string): boolean {
  const trimmed = href.trim()
  return isSafeNormalizedHref(stripControlCharacters(trimmed))
}

/**
 * Drop C0 controls and space.
 *
 * Done by codepoint rather than by regex: a regex holding literal control characters
 * is invisible in a diff and easy to break in a later edit. Browsers strip these
 * before resolving a scheme, so `java\nscript:alert(1)` navigates even though it does
 * not match a naive `startsWith("javascript:")` test.
 */
function stripControlCharacters(value: string): string {
  let out = ""
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0
    if (code > 0x20 && code !== 0x7f) out += character
  }
  return out
}

function isSafeNormalizedHref(value: string): boolean {
  const normalized = value.toLowerCase()
  if (normalized.startsWith("http://") || normalized.startsWith("https://")) return true
  // A protocol-relative `//evil.example` is a scheme-inheriting absolute URL, not a
  // path on this origin.
  return normalized.startsWith("/") && !normalized.startsWith("//")
}

const SECRET_PATTERNS: ReadonlyArray<{ label: string; pattern: RegExp }> = [
  { label: "bearer token", pattern: /\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*/gi },
  { label: "private key", pattern: /-----BEGIN [A-Z ]*PRIVATE KEY-----/g },
  {
    label: "credential-shaped key",
    pattern: /\b(?:sk|pk|rk|api)[-_](?:live|test|prod)?[-_]?[A-Za-z0-9]{20,}/g,
  },
  { label: "JWT", pattern: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g },
]

export interface SecretScan {
  text: string
  /** Labels of what was masked, for a visible warning. Empty means nothing found. */
  found: string[]
}

/**
 * Mask anything credential-shaped that survived export.
 *
 * A second line of defence, not the primary one — the SDK redacts before anything
 * leaves the instrumented process (see `proofstep.redaction`). This exists because
 * the dashboard also shows payloads ingested through the HTTP API by clients that
 * never ran that code, and because a redaction rule added today does not retroactively
 * clean what was stored yesterday.
 *
 * Deliberately not silent: masking without saying so would let someone conclude the
 * payload genuinely contained `***`.
 */
export function maskSecrets(text: string): SecretScan {
  let out = text
  const found: string[] = []
  for (const { label, pattern } of SECRET_PATTERNS) {
    // Fresh RegExp per call: a module-level /g regex carries `lastIndex` between
    // calls, so a shared instance would skip matches on every other payload.
    const regex = new RegExp(pattern.source, pattern.flags)
    if (regex.test(out)) {
      found.push(label)
      out = out.replace(new RegExp(pattern.source, pattern.flags), "«redacted at display»")
    }
  }
  return { text: out, found }
}

/** One-line preview for a table cell. */
export function preview(value: unknown, maxChars = 120): string {
  if (value === null || value === undefined) return ""
  const raw = typeof value === "string" ? value : (renderPayload(value).text ?? "")
  const flat = raw.replace(/\s+/g, " ").trim()
  return flat.length <= maxChars ? flat : `${flat.slice(0, maxChars - 1)}…`
}
