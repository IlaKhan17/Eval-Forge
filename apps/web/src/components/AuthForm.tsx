"use client"

/**
 * Sign in and sign up, in one component.
 *
 * The two forms differ by one field and one endpoint, and keeping them together is what stops them
 * drifting into two subtly different error-handling paths — which is exactly where a login form
 * starts leaking whether an account exists.
 */

import { ApiError, signIn } from "@/lib/api"
import Link from "next/link"
import { useRouter } from "next/navigation"
import { type FormEvent, useState } from "react"

export function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const router = useRouter()
  const [email, setEmail] = useState("")
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
        ...(signup && organization ? { organization } : {}),
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
    <div className="mx-auto mt-24 w-full max-w-sm">
      <h1 className="text-lg font-medium text-slate-100">
        {signup ? "Create an account" : "Sign in"}
      </h1>
      <p className="mt-1 text-xs text-slate-400">
        {signup
          ? "You get a workspace and a project to send your first trace to."
          : "Welcome back."}
      </p>

      <form onSubmit={submit} className="mt-6 space-y-3">
        <Field
          label="Email"
          type="email"
          value={email}
          onChange={setEmail}
          autoComplete="email"
          required
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
        {signup ? (
          <Field
            label="Organization"
            value={organization}
            onChange={setOrganization}
            hint="Optional — we will name it after your email if you leave it blank."
          />
        ) : null}

        {error ? (
          <p
            // `role="alert"` so a screen reader announces a failed sign-in rather than leaving the
            // person waiting for something that already happened.
            role="alert"
            className="rounded border border-red-900/50 bg-red-950/20 px-3 py-2 text-xs text-red-200"
          >
            {error}
          </p>
        ) : null}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-slate-100 px-3 py-2 text-sm font-medium text-slate-900 hover:bg-white disabled:opacity-50"
        >
          {busy ? "…" : signup ? "Create account" : "Sign in"}
        </button>
      </form>

      <p className="mt-4 text-xs text-slate-400">
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
      </p>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  hint,
  ...rest
}: {
  label: string
  value: string
  onChange: (value: string) => void
  type?: string
  hint?: string
  autoComplete?: string
  required?: boolean
}) {
  return (
    <label className="block text-xs">
      <span className="text-slate-400">{label}</span>
      <input
        {...rest}
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 w-full rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-600"
      />
      {hint ? <span className="mt-1 block text-slate-500">{hint}</span> : null}
    </label>
  )
}
