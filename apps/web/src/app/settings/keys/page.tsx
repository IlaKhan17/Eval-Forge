import { ApiKeySettings } from "@/components/ApiKeySettings"

export const metadata = { title: "API keys · EvalForge" }
export const dynamic = "force-dynamic"

export default function ApiKeysPage() {
  return <ApiKeySettings />
}
