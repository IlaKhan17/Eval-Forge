"use client"

import { MAX_RENDER_CHARS, maskSecrets, renderPayload } from "@/lib/sanitize"
import { useMemo, useState } from "react"

/**
 * Renders one captured payload.
 *
 * Text children only — never `dangerouslySetInnerHTML`. A span's output is untrusted
 * content by definition, and a Biome rule enforces the absence of that escape hatch so
 * this does not rely on anyone remembering.
 */
export function PayloadViewer({
  label,
  value,
  serverTruncated = false,
}: {
  label: string
  value: unknown
  /** The API could not return the payload — retention removed it, or storage failed. */
  serverTruncated?: boolean
}) {
  const [expanded, setExpanded] = useState(false)

  const rendered = useMemo(() => {
    const payload = renderPayload(value)
    const scanned = maskSecrets(payload.text)
    return { ...payload, text: scanned.text, secrets: scanned.found }
  }, [value])

  if (serverTruncated) {
    return (
      <Section label={label}>
        <p className="px-3 py-2 text-xs text-amber-300">
          The payload is not available. It was stored outside the database and the object could not
          be read — usually retention removed it. The span itself is intact.
        </p>
      </Section>
    )
  }

  if (value === null || value === undefined) {
    return (
      <Section label={label}>
        {/* "not captured" and "captured as null" are different facts, and the capture
            mode decides which one happened. */}
        <p className="px-3 py-2 text-xs text-slate-500">Not captured.</p>
      </Section>
    )
  }

  const body = expanded ? rendered.text : rendered.text.slice(0, 4_000)
  const clipped = !expanded && rendered.text.length > 4_000

  return (
    <Section
      label={label}
      action={
        <button
          type="button"
          onClick={() => navigator.clipboard?.writeText(rendered.text)}
          className="text-xs text-slate-400 hover:text-slate-200"
        >
          Copy
        </button>
      }
    >
      {rendered.secrets.length > 0 ? (
        <p className="border-b border-amber-900/50 bg-amber-950/30 px-3 py-1.5 text-xs text-amber-200">
          Masked at display: {rendered.secrets.join(", ")}. This payload reached the server
          unredacted — check the SDK's redaction rules for whatever produced it.
        </p>
      ) : null}

      <pre className="payload-body max-h-[28rem] overflow-auto px-3 py-2 text-slate-300">
        {body}
      </pre>

      {clipped || rendered.truncated ? (
        <div className="border-t border-slate-800 px-3 py-1.5 text-xs text-slate-400">
          {rendered.truncated ? (
            <>
              Showing the first {MAX_RENDER_CHARS.toLocaleString()} characters;{" "}
              {rendered.omittedChars.toLocaleString()} more are not rendered.
            </>
          ) : (
            <button type="button" onClick={() => setExpanded(true)} className="underline">
              Show all {rendered.text.length.toLocaleString()} characters
            </button>
          )}
        </div>
      ) : null}

      {rendered.lossy ? (
        <p className="border-t border-slate-800 px-3 py-1.5 text-xs text-slate-500">
          Some values could not be represented exactly (a cycle, or a very deep structure).
        </p>
      ) : null}
    </Section>
  )
}

function Section({
  label,
  action,
  children,
}: {
  label: string
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div className="rounded border border-slate-800">
      <header className="flex items-center justify-between border-b border-slate-800 px-3 py-1.5">
        <span className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</span>
        {action}
      </header>
      {children}
    </div>
  )
}
