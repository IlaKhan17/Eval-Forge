import { InviteAcceptor } from "@/components/InviteAcceptor"

export const metadata = { title: "Join a workspace · Proofstep" }

// The token comes from the query string, so there is nothing to prerender.
export const dynamic = "force-dynamic"

export default async function InvitePage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>
}) {
  const { token } = await searchParams
  // Read here rather than with `useSearchParams` in the component: that hook forces the whole tree
  // into a Suspense boundary, and the boundary's fallback would be a second loading state layered
  // over the one the component already has.
  return <InviteAcceptor token={token ?? null} />
}
