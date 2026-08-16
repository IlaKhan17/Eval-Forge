import { AuthForm } from "@/components/AuthForm"

export const metadata = { title: "Sign in · EvalForge" }

export default function LoginPage() {
  return <AuthForm mode="login" />
}
