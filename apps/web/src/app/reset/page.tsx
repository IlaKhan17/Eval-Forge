import { ResetForm } from "@/components/ResetForm"

export const metadata = { title: "Choose a new password · Proofstep" }

// The token is in the query string, so there is nothing to prerender.
export const dynamic = "force-dynamic"

export default async function ResetPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>
}) {
  const { token } = await searchParams
  return <ResetForm token={token ?? null} />
}
