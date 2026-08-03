import type { NextConfig } from "next"

/**
 * Security headers that do not vary per request.
 *
 * The Content-Security-Policy is *not* here — it needs a fresh nonce per response, so
 * it is set in `src/middleware.ts`. Putting a fixed nonce in a static header would be
 * worse than having no nonce at all: it would look like a strong policy while being
 * trivially satisfiable by injected markup.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,

  typescript: {
    // A type error must fail the build. The alternative ships a broken page and
    // discovers it in the browser.
    ignoreBuildErrors: false,
  },
  eslint: {
    // Linting is Biome's job here, run as its own CI step.
    ignoreDuringBuilds: true,
  },

  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "x-content-type-options", value: "nosniff" },
          // No referrer at all: a trace URL contains a trace id, and ids should not
          // leak to anywhere a link happens to point.
          { key: "referrer-policy", value: "no-referrer" },
          { key: "x-frame-options", value: "DENY" },
          // Nothing in a trace viewer needs a camera, a microphone, or a location.
          {
            key: "permissions-policy",
            value: "camera=(), microphone=(), geolocation=(), payment=()",
          },
          { key: "cross-origin-opener-policy", value: "same-origin" },
        ],
      },
    ]
  },
}

export default nextConfig
