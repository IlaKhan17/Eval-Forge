"use client"

/**
 * The page an invitation link lands on.
 *
 * Two audiences arrive here through the same URL, and they need different things:
 *
 * - **Someone with no account** — the common case, since being invited is usually how a person
 *   first hears of the product. They need to create one, and it must join the inviting organization
 *   rather than making a workspace of their own that nobody wanted.
 * - **Someone already signed in** — a second workspace, or a colleague who signed up first. They
 *   need one button.
 *
 * Which one you are is not something to ask; the session already knows. Everything else is decided
 * by the preview, which is why it is fetched before anything is rendered rather than after a
 * password has been typed into a form that was never going to work.
 *
 * The preview also carries the honesty of the page. An invitation form that cannot name the
 * organization or the address it is for is asking for a password with no explanation attached,
 * which is what a phishing page looks like.
 */

import { AuthForm } from "@/components/AuthForm"
import { AuthCard, FormError, SubmitButton } from "@/components/Field"
import {
  ApiError,
  type InvitePreview,
  type Me,
  acceptInvite,
  getMe,
  previewInvite,
} from "@/lib/api"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { type FormEvent, useEffect, useState } from "react"

type State =
  | { status: "loading" }
  | { status: "invalid"; detail: string }
  | { status: "ready"; preview: InvitePreview; me: Me | null }

export function InviteAcceptor({ token }: { token: string | null }) {
  const [state, setState] = useState<State>({ status: "loading" })

  useEffect(() => {
    if (!token) {
      setState({
        status: "invalid",
        detail: "This link is missing its invitation code. Ask whoever invited you to resend it.",
      })
      return
    }
    const controller = new AbortController()

    // Both, together. Rendering on the preview alone and then discovering the session would flash
    // a sign-up form at someone who is already signed in — and a form that appears and vanishes is
    // worse than a moment of "checking…".
    Promise.all([
      previewInvite(token, controller.signal),
      // A 401 here is the expected case, not a failure: most people following an invitation have no
      // account. Anything else is also treated as "not signed in", because the fallback — offering
      // to create an account — is the safe answer when the session is unknown.
      getMe(controller.signal).catch(() => null),
    ])
      .then(([preview, me]) => setState({ status: "ready", preview, me }))
      .catch((cause) => {
        if (cause instanceof ApiError && cause.status === 0) return // aborted on unmount
        setState({
          status: "invalid",
          detail:
            cause instanceof ApiError
              ? cause.detail
              : "That invitation could not be checked. Try again.",
        })
      })
    return () => controller.abort()
  }, [token])

  if (state.status === "loading") {
    return <AuthCard title="Checking your invitation…" />
  }

  if (state.status === "invalid") {
    return (
      <AuthCard title="This invitation cannot be used" subtitle={state.detail}>
        <p className="mt-6 text-xs text-slate-400">
          Invitations expire, and each one can be accepted once.{" "}
          <Link href="/login" className="text-slate-200 underline-offset-2 hover:underline">
            Sign in
          </Link>{" "}
          if you already have an account.
        </p>
      </AuthCard>
    )
  }

  const { preview, me } = state
  // Signed in as the invited person: one button, no forms.
  if (me && me.email.toLowerCase() === preview.email.toLowerCase()) {
    return <AcceptButton token={token as string} preview={preview} />
  }

  // Signed in as somebody else. Not an error — a shared computer, or two accounts — but it cannot
  // proceed, because only the invited address may spend the invitation.
  if (me) {
    return (
      <AuthCard
        title={`Invitation for ${preview.email}`}
        subtitle={`You are signed in as ${me.email}.`}
      >
        <p className="mt-6 text-xs text-slate-400">
          Only the address this invitation was sent to can accept it. Sign out and sign in as{" "}
          <span className="text-slate-200">{preview.email}</span>, or ask for an invitation to the
          address you use.
        </p>
      </AuthCard>
    )
  }

  // The common case: no account yet.
  return (
    <AuthForm
      mode="signup"
      invite={{ token: token as string, organization: preview.organization, email: preview.email }}
    />
  )
}

function AcceptButton({ token, preview }: { token: string; preview: InvitePreview }) {
  const router = useRouter()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await acceptInvite(token)
      router.replace("/traces")
      router.refresh()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : "That did not work. Try again.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthCard
      title={`Join ${preview.organization}`}
      subtitle={`You have been invited as a ${preview.role}.`}
    >
      <form onSubmit={submit} className="mt-6 space-y-3">
        {error ? <FormError>{error}</FormError> : null}
        <SubmitButton busy={busy}>Join {preview.organization}</SubmitButton>
      </form>
    </AuthCard>
  )
}
