import { AuthForm } from "@/components/AuthForm"
import { ForgotForm } from "@/components/ForgotForm"
import { InviteAcceptor } from "@/components/InviteAcceptor"
import { ResetForm } from "@/components/ResetForm"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

/**
 * The two flows that turn a single-user install into something a team can use: accepting an
 * invitation, and recovering an account nobody can get into.
 *
 * Both are mostly *routing between states*, and every one of those states is a place someone can
 * get stuck with no way out. So what is pinned here is which screen appears for which situation —
 * an expired invitation, a signed-in bystander, a truncated reset link — rather than the markup.
 *
 * Neither flow existed before: the members page told the inviter to share a `/invite?token=…` link
 * that resolved to a 404, and a forgotten password was a permanently lost account.
 */

const replace = vi.fn()
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, refresh: vi.fn(), push: vi.fn() }),
}))

interface StubbedRoute {
  match: string
  status?: number
  body: unknown
}

/** Route stubbing by substring, so a test says what it is answering rather than counting calls. */
function stubRoutes(routes: StubbedRoute[]): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const route = routes.find((candidate) => url.includes(candidate.match))
    if (!route) {
      return new Response(JSON.stringify({ detail: `unstubbed: ${url}` }), {
        status: 404,
        headers: { "content-type": "application/problem+json" },
      })
    }
    return new Response(JSON.stringify(route.body), {
      status: route.status ?? 200,
      headers: { "content-type": "application/json" },
    })
  })
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

const INVITE = {
  organization: "Acme",
  email: "invitee@example.com",
  role: "developer",
  expires_at: "2026-12-01T00:00:00Z",
}

beforeEach(() => {
  replace.mockClear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe("accepting an invitation", () => {
  it("offers to create an account when there is no session", async () => {
    stubRoutes([
      { match: "/invites/preview", body: INVITE },
      { match: "/auth/me", status: 401, body: { detail: "no session" } },
    ])

    render(<InviteAcceptor token="a-token" />)

    // Named, both of them. An invitation page that cannot say which workspace it is for, or which
    // address it was sent to, is a password form with no context — which is what a phishing page
    // looks like, and this one is asking to be trusted anyway.
    //
    // `findAllBy`: the organization is named in the subtitle *and* on the button, which is the
    // intent — the last thing you read before submitting should say what you are joining.
    expect((await screen.findAllByText(/Acme/)).length).toBeGreaterThan(0)
    const email = (await screen.findByLabelText(/Email/)) as HTMLInputElement
    expect(email.value).toBe("invitee@example.com")
  })

  it("fixes the email rather than merely prefilling it", async () => {
    // Only the invited address can spend the invitation. An editable field offers a change the API
    // will refuse — after the password has been typed.
    stubRoutes([
      { match: "/invites/preview", body: INVITE },
      { match: "/auth/me", status: 401, body: {} },
    ])

    render(<InviteAcceptor token="a-token" />)
    const email = (await screen.findByLabelText(/Email/)) as HTMLInputElement
    expect(email.readOnly).toBe(true)
  })

  it("sends the token with the sign-up so the account joins rather than creating a workspace", async () => {
    const fetchMock = stubRoutes([
      { match: "/invites/preview", body: INVITE },
      { match: "/auth/me", status: 401, body: {} },
      { match: "/api/auth/signup", body: { org_id: "org", project_id: "proj" } },
    ])

    render(<InviteAcceptor token="the-token" />)
    await screen.findByLabelText(/Email/)
    await userEvent.type(await screen.findByLabelText(/Password/), "a-long-enough-password")
    await userEvent.click(screen.getByRole("button", { name: /Join Acme/ }))

    await waitFor(() => {
      const signup = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/auth/signup"))
      expect(signup).toBeTruthy()
      expect(JSON.parse(String(signup?.[1]?.body)).invite_token).toBe("the-token")
    })
  })

  it("shows one button when the invited person is already signed in", async () => {
    stubRoutes([
      { match: "/invites/preview", body: INVITE },
      { match: "/auth/me", body: { id: "u", email: "invitee@example.com", organizations: [] } },
    ])

    render(<InviteAcceptor token="a-token" />)

    expect(await screen.findByRole("button", { name: /Join Acme/ })).toBeTruthy()
    // No password field: they have an account, and asking for one again would be asking them to
    // re-authenticate for no reason.
    expect(screen.queryByLabelText(/Password/)).toBeNull()
  })

  it("explains itself when a different account is signed in", async () => {
    // A shared computer, or two accounts. Not an error, but it cannot proceed — and silently
    // showing the join button would produce a 403 with no explanation attached.
    stubRoutes([
      { match: "/invites/preview", body: INVITE },
      {
        match: "/auth/me",
        body: { id: "u", email: "someone.else@example.com", organizations: [] },
      },
    ])

    render(<InviteAcceptor token="a-token" />)

    expect(await screen.findByText(/someone.else@example.com/)).toBeTruthy()
    expect(screen.queryByRole("button", { name: /Join/ })).toBeNull()
  })

  it("says an expired invitation is expired, and offers a way on", async () => {
    stubRoutes([
      {
        match: "/invites/preview",
        status: 404,
        body: { detail: "That invitation has expired. Ask for a new one." },
      },
      { match: "/auth/me", status: 401, body: {} },
    ])

    render(<InviteAcceptor token="stale" />)

    expect(await screen.findByText(/has expired/)).toBeTruthy()
    expect(screen.getByRole("link", { name: /Sign in/ })).toBeTruthy()
  })

  it("does not send a request for a link with no token at all", async () => {
    const fetchMock = stubRoutes([])
    render(<InviteAcceptor token={null} />)

    expect(await screen.findByText(/missing its invitation code/)).toBeTruthy()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe("resetting a password", () => {
  it("confirms without claiming an email was sent", async () => {
    // The API answers identically whether or not the address exists, because a different answer is
    // a membership oracle. "Check your inbox" would be a small lie that leaves someone who mistyped
    // their address refreshing an empty one.
    const detail = "If that address has an account, a reset link is on its way."
    stubRoutes([{ match: "/api/auth/forgot", body: { detail } }])

    render(<ForgotForm />)
    await userEvent.type(screen.getByLabelText(/Email/), "someone@example.com")
    await userEvent.click(screen.getByRole("button", { name: /Send a reset link/ }))

    expect(await screen.findByText(new RegExp(detail.slice(0, 30)))).toBeTruthy()
  })

  it("mentions that a self-hosted install may have no mail server", async () => {
    stubRoutes([{ match: "/api/auth/forgot", body: { detail: "acknowledged" } }])

    render(<ForgotForm />)
    await userEvent.type(screen.getByLabelText(/Email/), "someone@example.com")
    await userEvent.click(screen.getByRole("button", { name: /Send a reset link/ }))

    expect(await screen.findByText(/no mail server/)).toBeTruthy()
  })

  it("catches a mistyped confirmation before sending it", async () => {
    // The one failure this flow exists to fix, recreated by the flow itself: a password that both
    // the form and the server accept, and that the person cannot reproduce.
    const fetchMock = stubRoutes([{ match: "/api/auth/reset", body: { detail: "changed" } }])

    render(<ResetForm token="a-token" />)
    await userEvent.type(screen.getByLabelText(/New password/), "a-long-enough-password")
    await userEvent.type(screen.getByLabelText(/Confirm/), "a-different-password")
    await userEvent.click(screen.getByRole("button", { name: /Change password/ }))

    expect(await screen.findByText(/do not match/)).toBeTruthy()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("refuses a password too short for the API to accept", async () => {
    const fetchMock = stubRoutes([{ match: "/api/auth/reset", body: { detail: "changed" } }])

    render(<ResetForm token="a-token" />)
    await userEvent.type(screen.getByLabelText(/New password/), "short")
    await userEvent.type(screen.getByLabelText(/Confirm/), "short")
    await userEvent.click(screen.getByRole("button", { name: /Change password/ }))

    // The error, not the hint under the field — which says the same thing, and would let this
    // assertion pass with the validation removed entirely.
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      expect.stringMatching(/at least 12 characters/i),
    )
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it("sends the person to sign in rather than signing them in", async () => {
    // A reset that handed back a session would mean one stolen link is a session. The login proves
    // the new password reached the person who set it.
    stubRoutes([{ match: "/api/auth/reset", body: { detail: "changed" } }])

    render(<ResetForm token="a-token" />)
    await userEvent.type(screen.getByLabelText(/New password/), "a-long-enough-password")
    await userEvent.type(screen.getByLabelText(/Confirm/), "a-long-enough-password")
    await userEvent.click(screen.getByRole("button", { name: /Change password/ }))

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login?reset=1"))
  })

  it("explains a truncated link instead of failing on submit", async () => {
    // Email clients and chat apps break long URLs. Showing the form and failing after a password
    // has been chosen is the worse version of this.
    render(<ResetForm token={null} />)

    expect(screen.getByText(/missing the code/)).toBeTruthy()
    expect(screen.getByRole("link", { name: /Ask for a new one/ })).toBeTruthy()
  })
})

describe("finding the way to a reset", () => {
  it("is linked from the sign-in form", async () => {
    // Discoverability is the whole feature. A reset flow nobody can find from the screen where they
    // are stuck is not a reset flow.
    render(<AuthForm mode="login" />)
    expect(screen.getByRole("link", { name: /Forgot password/ })).toBeTruthy()
  })
})
