import { ExperimentDetailView } from "@/components/ExperimentDetail"

export const metadata = { title: "Experiment · Proofstep" }

export const dynamic = "force-dynamic"

export default async function ExperimentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params
  return <ExperimentDetailView experimentId={id} />
}
