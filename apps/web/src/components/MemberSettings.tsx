"use client"

/**
 * Who is in the workspace, and what they can do.
 *
 * The invitation flow shows the token rather than sending an email. A self-hosted install has no
 * mail server, and making one a hard requirement to add a colleague would mean the product does not
 * work until someone configures SMTP. The cloud deployment sends the link; here the person who
 * invited copies it. Stated in the UI so nobody waits for an email that was never sent.
 *
 * The link is offered as a copy button rather than as text to select, because these are long and a
 * partial selection produces a token that resolves to nothing — indistinguishable, from the
 * recipient's side, from an invitation that was never valid.
 */

import { ErrorState, Panel, Skeleton } from "@/components/Primitives"
import { type Member, changeRole, getMe, inviteMember, listMembers, removeMember } from "@/lib/api"
import { formatTimestamp } from "@/lib/format"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

/**
 * The link a colleague follows.
 *
 * Built from the browser's own origin rather than from configuration: the dashboard is reached at
 * whatever address this person just used, and that is by definition an address that works. A
 * configured base URL would be the more "correct" source and would hand out a link to a hostname
 * the recipient may not be able to resolve.
 */
function inviteUrl(token: string): string {
  const origin = typeof window === "undefined" ? "" : window.location.origin
  return `${origin}/invite?token=${encodeURIComponent(token)}`
}

const ROLES = ["admin", "developer", "reviewer", "viewer"] as const

/** What each role can do, in the words someone choosing one would use. */
const ROLE_HELP: Record<string, string> = {
  owner: "Everything, and cannot be removed by anyone else.",
  admin: "Manage members, projects, and keys.",
  developer: "Create datasets, evaluators, policies, and runs.",
  reviewer: "Read everything and annotate traces.",
  viewer: "Read only.",
}

export function MemberSettings() {
  const client = useQueryClient()
  const [email, setEmail] = useState("")
  const [role, setRole] = useState<string>("developer")
  const [inviteLink, setInviteLink] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  const me = useQuery({ queryKey: ["me"], queryFn: ({ signal }) => getMe(signal) })
  const orgId = me.data?.organizations[0]?.org_id
  const myRole = me.data?.organizations[0]?.role
  const canManage = myRole === "owner" || myRole === "admin"

  const members = useQuery({
    queryKey: ["members", orgId],
    queryFn: ({ signal }) => listMembers(orgId as string, signal),
    enabled: Boolean(orgId),
  })

  const invite = useMutation({
    mutationFn: () => inviteMember(orgId as string, email, role),
    onSuccess: (created) => {
      setInviteLink(created.token ?? null)
      setEmail("")
      client.invalidateQueries({ queryKey: ["members", orgId] })
    },
  })

  const update = useMutation({
    mutationFn: ({ userId, next }: { userId: string; next: string }) =>
      changeRole(orgId as string, userId, next),
    onSuccess: () => client.invalidateQueries({ queryKey: ["members", orgId] }),
  })

  const remove = useMutation({
    mutationFn: (userId: string) => removeMember(orgId as string, userId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["members", orgId] }),
  })

  if (me.isPending) return <Skeleton rows={8} />
  if (me.isError) return <ErrorState error={me.error} onRetry={() => me.refetch()} />

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-medium text-slate-100">Members</h1>
        <p className="mt-1 text-xs text-slate-400">
          A role applies to every project in this organization.
        </p>
      </div>

      {inviteLink ? (
        <div className="rounded border border-emerald-900/50 bg-emerald-950/20 px-4 py-3">
          <p className="text-xs text-emerald-200">
            Send this link to your colleague. It only works for the address you invited, and it
            expires in 14 days.
          </p>
          <code className="mt-2 block break-all font-mono text-xs text-slate-100">
            {inviteUrl(inviteLink)}
          </code>
          <div className="mt-2 flex items-center gap-3">
            <button
              type="button"
              onClick={() => {
                // `?.` because clipboard access is unavailable outside a secure context — an
                // install reached over plain http on a LAN, which is a normal way to try this
                // product. The link is on screen either way; the button just stops being useful.
                navigator.clipboard?.writeText(inviteUrl(inviteLink)).then(
                  () => setCopied(true),
                  () => setCopied(false),
                )
              }}
              className="rounded border border-emerald-800 px-2 py-1 text-xs text-emerald-200 hover:bg-emerald-950/40"
            >
              {copied ? "Copied" : "Copy link"}
            </button>
            <button
              type="button"
              onClick={() => {
                setInviteLink(null)
                setCopied(false)
              }}
              className="text-xs text-emerald-300 underline-offset-2 hover:underline"
            >
              Done
            </button>
          </div>
        </div>
      ) : null}

      {canManage ? (
        <Panel title="Invite someone">
          <div className="flex flex-wrap items-end gap-3 px-4 py-3">
            <label className="min-w-56 flex-1 text-xs">
              <span className="text-slate-400">Email</span>
              <input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="colleague@example.com"
                className="mt-1 w-full rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 outline-none focus:border-slate-600"
              />
            </label>
            <label className="text-xs">
              <span className="text-slate-400">Role</span>
              <select
                value={role}
                onChange={(event) => setRole(event.target.value)}
                className="mt-1 block rounded border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100"
              >
                {ROLES.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              disabled={invite.isPending || !email}
              onClick={() => invite.mutate()}
              className="rounded bg-slate-100 px-3 py-2 text-xs font-medium text-slate-900 hover:bg-white disabled:opacity-50"
            >
              {invite.isPending ? "…" : "Invite"}
            </button>
          </div>
          <p className="px-4 pb-3 text-xs text-slate-500">{ROLE_HELP[role]}</p>
          {invite.isError ? (
            <p role="alert" className="px-4 pb-3 text-xs text-red-300">
              {(invite.error as Error).message}
            </p>
          ) : null}
        </Panel>
      ) : null}

      <Panel title={`Members · ${members.data?.length ?? 0}`}>
        {members.isPending ? (
          <Skeleton rows={4} />
        ) : (
          <ul className="divide-y divide-slate-800">
            {(members.data ?? []).map((member) => (
              <MemberRow
                key={member.user_id}
                member={member}
                canManage={canManage}
                isSelf={member.user_id === me.data?.id}
                onRole={(next) => update.mutate({ userId: member.user_id, next })}
                onRemove={() => remove.mutate(member.user_id)}
              />
            ))}
          </ul>
        )}
      </Panel>
    </div>
  )
}

function MemberRow({
  member,
  canManage,
  isSelf,
  onRole,
  onRemove,
}: {
  member: Member
  canManage: boolean
  isSelf: boolean
  onRole: (role: string) => void
  onRemove: () => void
}) {
  // An owner's row is read-only here. Demotion and removal of the last owner are refused by the API
  // anyway; showing controls that will be rejected is worse than not showing them.
  const editable = canManage && member.role !== "owner" && !isSelf

  return (
    <li className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-xs">
      <div>
        <div className="text-slate-200">
          {member.name ?? member.email}
          {isSelf ? <span className="ml-2 text-slate-500">you</span> : null}
        </div>
        <div className="mt-0.5 text-slate-500">
          {member.email} · joined {formatTimestamp(member.joined_at)}
        </div>
      </div>
      <div className="flex items-center gap-2">
        {editable ? (
          <select
            value={member.role}
            onChange={(event) => onRole(event.target.value)}
            className="rounded border border-slate-800 bg-slate-900/60 px-2 py-1 text-slate-200"
          >
            {ROLES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        ) : (
          <span className="rounded border border-slate-700 px-2 py-1 text-slate-300">
            {member.role}
          </span>
        )}
        {editable ? (
          <button
            type="button"
            onClick={onRemove}
            className="rounded border border-slate-700 px-2 py-1 text-slate-300 hover:border-red-800 hover:text-red-300"
          >
            Remove
          </button>
        ) : null}
      </div>
    </li>
  )
}
