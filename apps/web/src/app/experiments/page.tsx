import { ExperimentList } from "@/components/ExperimentList"

export const metadata = { title: "Experiments · EvalForge" }

// Published runs arrive continuously; there is nothing here worth prerendering.
export const dynamic = "force-dynamic"

export default function ExperimentsPage() {
  return <ExperimentList />
}
