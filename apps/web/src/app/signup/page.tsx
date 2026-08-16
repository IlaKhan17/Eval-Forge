import { AuthForm } from "@/components/AuthForm"

export const metadata = { title: "Create an account · Proofstep" }

export default function SignupPage() {
  return <AuthForm mode="signup" />
}
