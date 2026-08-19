"use client"

/**
 * Sign in and sign up, in one component.
 *
 * The two forms differ by one field and one endpoint, and keeping them together is what stops them
 * drifting into two subtly different error-handling paths — which is exactly where a login form
 * starts leaking whether an account exists.
 *
 * It also serves the invitation flow. `invite` fixes the email to the address the invitation was
 * sent to and hides the organization field, because someone joining a workspace is not creating
 * one; the token rides along so the account lands in the inviting organization rather than in a new
 * one of its own.
 */

import { AuthCard, Field, FormError, SubmitButton } from "@/components/Field"
import { ApiError, signIn } from "@/lib/api"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { type FormEvent, useState } from "react"

export interface InviteContext {
  token: string
  organization: string
  /** The address the invitation names. The only one that can spend it. */
  email: string
}

export function AuthForm({
  mode,
  invite,
}: {
  mode: "login" | "signup"
  invite?: InviteContext
}) {
  const router = useRouter()
  const [email, setEmail] = useState(invite?.email ?? "")
  const [password, setPassword] = useState("")
  const [organization, setOrganization] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const signup = mode === "signup"

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await signIn(mode, {
        email,
        password,
        ...(signup && organization && !invite ? { organization } : {}),
        ...(signup && invite ? { invite_token: invite.token } : {}),
      })
      // `replace`, not `push`: the login page should not be somewhere the back button returns to
      // once a session exists.
      router.replace("/traces")
      router.refresh()
    } catch (cause) {
      // The API's own message. It already draws the line between "that email is taken" and "those
      // credentials do not match" deliberately, and rewording it here would blur that.
      setError(cause instanceof ApiError ? cause.detail : "Something went wrong. Try again.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthCard
      title={signup ? "Create an account" : "Sign in"}
      subtitle={
        invite ? (
          <>
            You have been invited to <span className="text-slate-200">{invite.organization}</span>.
          </>
        ) : signup ? (
          "You get a workspace and a project to send your first trace to."
        ) : (
          "Welcome back."
        )
      }
    >
      <form onSubmit={submit} className="mt-6 space-y-3">
        <Field
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          required
          // Fixed, not merely prefilled. Only the invited address can spend the invitation, so an
          // editable field here offers a change that the API will refuse — after the password has
          // been typed.
          readOnly={Boolean(invite)}
          hint={invite ? "The address this invitation was sent to." : undefined}
        />
        <Field
          label="Password"
          type="password"
          value={password}
          onChange={setPassword}
          autoComplete={signup ? "new-password" : "current-password"}
          required
          hint={signup ? "At least 12 characters. Length is what actually helps." : undefined}
        />
        {signup && !invite ? (
          <Field
            label="Organization"
            value={organization}
            onChange={setOrganization}
            hint="Optional — we will name it after your email if you leave it blank."
          />
        ) : null}

        {error ? <FormError>{error}</FormError> : null}

        <SubmitButton busy={busy}>
          {invite ? `Join ${invite.organization}` : signup ? "Create account" : "Sign in"}
        </SubmitButton>
      </form>

      {invite ? null : (
        <p className="mt-4 flex justify-between text-xs text-slate-400">
          <span>
            {signup ? (
              <>
                Already have an account?{" "}
                <Link href="/login" className="text-slate-200 underline-offset-2 hover:underline">
                  Sign in
                </Link>
              </>
            ) : (
              <>
                No account?{" "}
                <Link href="/signup" className="text-slate-200 underline-offset-2 hover:underline">
                  Create one
                </Link>
              </>
            )}
          </span>
          {signup ? null : (
            <Link href="/forgot" className="text-slate-400 underline-offset-2 hover:underline">
              Forgot password?
            </Link>
          )}
        </p>
      )}
    </AuthCard>
  )
}
