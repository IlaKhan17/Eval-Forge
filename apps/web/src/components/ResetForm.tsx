"use client"

/**
 * Choose a new password, using the token from a reset link.
 *
 * Two decisions worth keeping:
 *
 * **It does not sign you in.** The API deliberately returns no session here, so this page sends the
 * person to the login screen. Handing back a session would be friendlier and would also mean a
 * single stolen link is a live session; making them log in proves the new password reached the
 * person who set it.
 *
 * **The confirmation field is checked here, not by the API.** A mistyped password that both the
 * form and the server accept locks someone out of the account they were in the middle of
 * recovering — the one failure this whole flow exists to fix.
 */

import { AuthCard, Field, FormError, SubmitButton } from "@/components/Field"
import { ApiError, completePasswordReset } from "@/lib/api"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { type FormEvent, useState } from "react"

const MIN_LENGTH = 12

export function ResetForm({ token }: { token: string | null }) {
  const router = useRouter()
  const [password, setPassword] = useState("")
  const [confirmation, setConfirmation] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (!token) {
    return (
      <AuthCard
        title="This link is incomplete"
        subtitle="It is missing the code that identifies your reset request."
      >
        <p className="mt-6 text-xs text-slate-400">
          Links can be truncated by email clients and chat apps.{" "}
          <Link href="/forgot" className="text-slate-200 underline-offset-2 hover:underline">
            Ask for a new one
          </Link>
          .
        </p>
      </AuthCard>
    )
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (password !== confirmation) {
      setError("Those two passwords do not match.")
      return
    }
    if (password.length < MIN_LENGTH) {
      setError(`Use at least ${MIN_LENGTH} characters.`)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await completePasswordReset(token as string, password)
      // `replace`: the reset link is spent, so the back button must not return to a form that can
      // no longer work.
      router.replace("/login?reset=1")
      router.refresh()
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause.detail
          : "That reset did not go through. Ask for a new link.",
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthCard title="Choose a new password" subtitle="You will sign in with it on the next screen.">
      <form onSubmit={submit} className="mt-6 space-y-3">
        <Field
          label="New password"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          required
          autoFocus
          hint={`At least ${MIN_LENGTH} characters. Length is what actually helps.`}
        />
        <Field
          label="Confirm new password"
          type="password"
          value={confirmation}
          onChange={setConfirmation}
          autoComplete="new-password"
          required
        />
        {error ? <FormError>{error}</FormError> : null}
        <SubmitButton busy={busy}>Change password</SubmitButton>
      </form>

      <p className="mt-4 text-xs text-slate-500">
        Every session on this account is signed out when the password changes, including any an
        attacker had.
      </p>
    </AuthCard>
  )
}
