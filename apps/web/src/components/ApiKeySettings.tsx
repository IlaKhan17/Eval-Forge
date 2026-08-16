"use client"

/**
 * Where a person gets the credential they paste into their application.
 *
 * The one interaction that matters here is the reveal: a key is shown exactly once, at creation,
 * because only its SHA-256 digest is stored. That is a real constraint of the design rather than an
 * inconvenience, so the UI states it plainly at the moment it applies — a key panel that quietly
 * hides the value and offers no explanation gets people copying it out of a network tab.
 */

import { ErrorState, Panel, Skeleton } from "@/components/Primitives"
import {
  type ApiKeySummary,
  createApiKey,
  getMe,
  listApiKeys,
  listProjects,
  revokeApiKey,
} from "@/lib/api"
import { formatTimestamp } from "@/lib/format"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

const SCOPES = ["ingest", "read", "write", "annotate"] as const

export function ApiKeySettings() {
  const client = useQueryClient()
  const [name, setName] = useState("")
  const [scopes, setScopes] = useState<string[]>(["ingest", "read"])
  const [revealed, setRevealed] = useState<string | null>(null)

  const me = useQuery({ queryKey: ["me"], queryFn: ({ signal }) => getMe(signal) })
  const orgId = me.data?.organizations[0]?.org_id

  const projects = useQuery({
    queryKey: ["projects", orgId],
    queryFn: ({ signal }) => listProjects(orgId as string, signal),
    enabled: Boolean(orgId),
  })
  const projectId = projects.data?.[0]?.id

  const keys = useQuery({
    queryKey: ["api-keys", projectId],
    queryFn: ({ signal }) => listApiKeys(projectId as string, signal),
    enabled: Boolean(projectId),
  })

  const create = useMutation({
    mutationFn: () => createApiKey(projectId as string, { name: name || "default", scopes }),
    onSuccess: (created) => {
      // Held in component state, never refetched — the server cannot return it again.
      setRevealed(created.token ?? null)
      setName("")
      client.invalidateQueries({ queryKey: ["api-keys", projectId] })
    },
  })

  const revoke = useMutation({
    mutationFn: (keyId: string) => revokeApiKey(projectId as string, keyId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["api-keys", projectId] }),
  })

  if (me.isPending || projects.isPending) return <Skeleton rows={8} />
  if (me.isError) return <ErrorState error={me.error} onRetry={() => me.refetch()} />

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-medium text-slate-100">API keys</h1>
        <p className="mt-1 text-xs text-slate-400">
          Paste one into your application or CI job. Scopes cap what a key can do independently of
          your role — an ingest-only key that leaks cannot read the traces back.
        </p>
      </div>

      {revealed ? (
        <div className="rounded border border-emerald-900/50 bg-emerald-950/20 px-4 py-3">
          <p className="text-xs text-emerald-200">
            Copy this now. It is stored only as a hash, so it cannot be shown again.
          </p>
          <code className="mt-2 block break-all font-mono text-xs text-slate-100">{revealed}</code>
          <button
            type="button"
            onClick={() => setRevealed(null)}
            className="mt-2 text-xs text-emerald-300 underline-offset-2 hover:underline"
          >
            I have saved it
          </button>
        </div>
      ) : null}

      <Panel title="Create a key">
        <div className="space-y-3 px-4 py-3">
          <label className="block text-xs">
            <span className="text-slate-400">Name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="ci"
              className="mt-1 w-full rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-600"
            />
          </label>

          <fieldset className="text-xs">
            <legend className="text-slate-400">Scopes</legend>
            <div className="mt-1 flex flex-wrap gap-3">
              {SCOPES.map((scope) => (
                <label key={scope} className="flex items-center gap-1.5 text-slate-300">
                  <input
                    type="checkbox"
                    checked={scopes.includes(scope)}
                    onChange={(event) =>
                      setScopes((current) =>
                        event.target.checked
                          ? [...current, scope]
                          : current.filter((item) => item !== scope),
                      )
                    }
                  />
                  {scope}
                </label>
              ))}
            </div>
          </fieldset>

          <button
            type="button"
            disabled={create.isPending || scopes.length === 0 || !projectId}
            onClick={() => create.mutate()}
            className="rounded bg-slate-100 px-3 py-1.5 text-xs font-medium text-slate-900 hover:bg-white disabled:opacity-50"
          >
            {create.isPending ? "…" : "Create key"}
          </button>
          {create.isError ? (
            <p role="alert" className="text-xs text-red-300">
              {(create.error as Error).message}
            </p>
          ) : null}
        </div>
      </Panel>

      <Panel title={`Keys · ${keys.data?.length ?? 0}`}>
        {keys.isPending ? (
          <Skeleton rows={4} />
        ) : keys.data && keys.data.length > 0 ? (
          <ul className="divide-y divide-slate-800">
            {keys.data.map((key) => (
              <KeyRow key={key.id} apiKey={key} onRevoke={() => revoke.mutate(key.id)} />
            ))}
          </ul>
        ) : (
          <p className="px-4 py-8 text-center text-sm text-slate-400">
            No keys yet. Create one above, then set <code>PROOFSTEP_API_KEY</code> in your app.
          </p>
        )}
      </Panel>
    </div>
  )
}

function KeyRow({ apiKey, onRevoke }: { apiKey: ApiKeySummary; onRevoke: () => void }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-xs">
      <div>
        <div className="flex items-center gap-2">
          <span className="text-slate-200">{apiKey.name}</span>
          {/* The prefix identifies which key this is in a list and authenticates as nothing. */}
          <code className="font-mono text-slate-500">{apiKey.prefix}</code>
        </div>
        <div className="mt-0.5 text-slate-500">
          {apiKey.scopes.join(", ")} · created {formatTimestamp(apiKey.created_at)} ·{" "}
          {apiKey.last_used_at ? `last used ${formatTimestamp(apiKey.last_used_at)}` : "never used"}
        </div>
      </div>
      <button
        type="button"
        onClick={onRevoke}
        className="rounded border border-slate-700 px-2 py-1 text-slate-300 hover:border-red-800 hover:text-red-300"
      >
        Revoke
      </button>
    </li>
  )
}
