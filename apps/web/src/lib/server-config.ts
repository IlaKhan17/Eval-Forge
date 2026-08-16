/**
 * Server-only configuration.
 *
 * Nothing here may be imported from a client component. The names deliberately lack
 * the `NEXT_PUBLIC_` prefix, which is what keeps Next.js from inlining them into the
 * browser bundle — that prefix is the whole boundary, so it is worth stating that its
 * absence is intentional rather than an oversight.
 */

export interface ServerConfig {
  apiUrl: string
  /**
   * Legacy single-key mode, and no longer how the dashboard authenticates.
   *
   * The proxy carries the signed-in user's session instead. This is kept only so an existing
   * `.env.local` does not fail to boot, and nothing reads it — a dashboard that fell back to a
   * shared key when nobody was signed in would hand an anonymous visitor whatever that key can do.
   */
  apiKey?: string
}

export class ConfigError extends Error {}

let cached: ServerConfig | null = null

/**
 * Read and validate the API connection settings.
 *
 * Validated on first use rather than at module load: a missing key should produce a
 * clear error on the request that needed it, not a build that fails for reasons the
 * message does not explain.
 */
export function serverConfig(): ServerConfig {
  if (cached) return cached

  const apiUrl = process.env.PROOFSTEP_API_URL?.trim()
  const apiKey = process.env.PROOFSTEP_API_KEY?.trim()

  if (!apiUrl) {
    throw new ConfigError(
      "PROOFSTEP_API_URL is not set. Point it at the API, e.g. http://localhost:8000",
    )
  }
  let parsed: URL
  try {
    parsed = new URL(apiUrl)
  } catch {
    throw new ConfigError(`PROOFSTEP_API_URL is not a valid URL: ${apiUrl}`)
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new ConfigError(`PROOFSTEP_API_URL must be http or https, got ${parsed.protocol}`)
  }

  cached = { apiUrl: parsed.origin, apiKey }
  return cached
}

/** Test seam: the cache would otherwise outlive an environment change in a suite. */
export function resetServerConfig(): void {
  cached = null
}
