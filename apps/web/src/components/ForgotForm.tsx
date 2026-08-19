"use client"

/**
 * Ask for a password reset link.
 *
 * The interesting decision is what this screen says afterwards. The API answers identically whether
 * or not the address has an account — a different answer turns the form into a membership oracle
 * against any email list — so the honest confirmation is conditional: *if* that address has an
 * account, something is on its way. Writing "check your inbox" instead would be a small lie that
 * leaves someone who typo'd their address waiting for a mail that was never sent to anyone.
 *
 * It also says the quiet part about delivery. There is no mail transport in a self-hosted install,
 * so for most deployments the link reaches a person through whoever runs the server. Telling them
 * that up front beats letting them refresh an empty inbox.
 */

import { AuthCard, Field, FormError, FormNotice, SubmitButton } from "@/components/Field"
import { ApiError, requestPasswordReset } from "@/lib/api"
import Link from "next/link"
import { type FormEvent, useState } from "react"

export function ForgotForm() {
  const [email, setEmail] = useState("")
  const [sent, setSent] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const { detail } = await requestPasswordReset(email)
      // The API's own wording, which is deliberately non-committal for the reason above.
      setSent(detail)
    } catch (cause) {
      setError(
        cause instanceof ApiError ? cause.detail : "That request did not go through. Try again.",
      )
    } finally {
      setBusy(false)
    }
  }

  if (sent) {
    return (
      <AuthCard title="Check your email">
        <div className="mt-6 space-y-3">
          <FormNotice>{sent}</FormNotice>
          <p className="text-xs text-slate-500">
            Self-hosted installations often have no mail server configured. If nothing arrives, ask
            whoever runs this one — they can issue a link directly.
          </p>
          <p className="text-xs text-slate-400">
            <Link href="/login" className="text-slate-200 underline-offset-2 hover:underline">
              Back to sign in
            </Link>
          </p>
        </div>
      </AuthCard>
    )
  }

  return (
    <AuthCard
      title="Reset your password"
      subtitle="We will send a link to the address on your account."
    >
      <form onSubmit={submit} className="mt-6 space-y-3">
        <Field
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          required
          autoFocus
        />
        {error ? <FormError>{error}</FormError> : null}
        <SubmitButton busy={busy}>Send a reset link</SubmitButton>
      </form>

      <p className="mt-4 text-xs text-slate-400">
        Remembered it?{" "}
        <Link href="/login" className="text-slate-200 underline-offset-2 hover:underline">
          Sign in
        </Link>
      </p>
    </AuthCard>
  )
}
