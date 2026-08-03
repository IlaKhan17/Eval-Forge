import { TraceDetailView } from "@/components/TraceDetail"

export const dynamic = "force-dynamic"

export default async function TracePage({
  params,
}: {
  params: Promise<{ traceId: string }>
}) {
  const { traceId } = await params
  // Decoded once here: the segment arrives percent-encoded, and an id containing a
  // colon (OTLP clients emit them) would otherwise be re-encoded on the way to the API
  // and match nothing.
  return <TraceDetailView traceId={decodeURIComponent(traceId)} />
}
