"use client"

/**
 * A labelled text input, shared by every form that asks for a credential.
 *
 * Extracted from `AuthForm` when the invitation and password-reset pages arrived. Four forms with
 * four copies of the same markup is how one of them ends up without an `autoComplete` hint, or with
 * a label that is a `<div>` and so is not read out by anything.
 */

export function Field({
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
  readOnly?: boolean
  autoFocus?: boolean
}) {
  return (
    <label className="block text-xs">
      <span className="text-slate-400">{label}</span>
      <input
        {...rest}
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 w-full rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-600 read-only:text-slate-400"
      />
      {hint ? <span className="mt-1 block text-slate-500">{hint}</span> : null}
    </label>
  )
}

/**
 * A failed submission.
 *
 * `role="alert"` so a screen reader announces it. Without that, a failed sign-in is silence for
 * anyone not watching the pixels — they wait for something that already happened.
 */
export function FormError({ children }: { children: React.ReactNode }) {
  return (
    <p
      role="alert"
      className="rounded border border-red-900/50 bg-red-950/20 px-3 py-2 text-xs text-red-200"
    >
      {children}
    </p>
  )
}

/** A successful submission, in the same shape so the two never jump the layout. */
export function FormNotice({ children }: { children: React.ReactNode }) {
  // `<output>` rather than a `<p role="status">`: it carries the same live-region semantics as the
  // role, and the lint rule that insists on it is right — a real element beats a role attribute
  // that someone can drop in a refactor without noticing what it was doing.
  return (
    <output className="block rounded border border-slate-800 bg-slate-900/40 px-3 py-2 text-xs text-slate-300">
      {children}
    </output>
  )
}

export function SubmitButton({ busy, children }: { busy: boolean; children: React.ReactNode }) {
  return (
    <button
      type="submit"
      disabled={busy}
      className="w-full rounded bg-slate-100 px-3 py-2 text-sm font-medium text-slate-900 hover:bg-white disabled:opacity-50"
    >
      {busy ? "…" : children}
    </button>
  )
}

/** The card every one of these forms sits in. */
export function AuthCard({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: React.ReactNode
  /** Optional: a bare heading is a legitimate state — "checking…" has nothing to put under it. */
  children?: React.ReactNode
}) {
  return (
    <div className="mx-auto mt-24 w-full max-w-sm">
      <h1 className="text-lg font-medium text-slate-100">{title}</h1>
      {subtitle ? <p className="mt-1 text-xs text-slate-400">{subtitle}</p> : null}
      {children}
    </div>
  )
}
