import { Skeleton } from "@/components/Primitives"
import { TraceList } from "@/components/TraceList"
import { Suspense } from "react"

export const metadata = { title: "Traces · Proofstep" }

// Filters come from the URL and the data is live, so there is nothing to prerender.
export const dynamic = "force-dynamic"

export default function TracesPage() {
  return (
    // `useSearchParams` suspends, and without a boundary the whole route would fall
    // back to client rendering with no visible loading state.
    <Suspense fallback={<Skeleton />}>
      <TraceList />
    </Suspense>
  )
}
