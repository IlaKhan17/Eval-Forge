"use client"

import { ApiError } from "@/lib/api"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { type ReactNode, useState } from "react"

/**
 * React Query configuration.
 *
 * The retry policy is the part with real consequences. Retrying a 401 or a 404 is
 * pointless — three attempts turn a clear "not authenticated" into a slow one — and
 * retrying a 429 immediately makes the rate limit worse. Only genuinely transient
 * failures are retried, and the decision lives on `ApiError.isTransient` so it is
 * stated once.
 */
export function QueryProvider({ children }: { children: ReactNode }) {
  // Created in state, not at module scope: a module-level client is shared across
  // requests on the server, which would leak one user's cached data into another's
  // render.
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Traces are immutable once complete, so a short stale window costs
            // nothing and stops a re-render storm from refetching everything.
            staleTime: 15_000,
            gcTime: 5 * 60_000,
            refetchOnWindowFocus: false,
            retry: (attempt, error) =>
              attempt < 2 && (!(error instanceof ApiError) || error.isTransient),
          },
        },
      }),
  )

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}
