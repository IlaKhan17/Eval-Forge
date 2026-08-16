"""The self-service account lifecycle: sign up, invite a colleague, mint a key, use it.

This is the path that turns a self-hosted service into a product. It is tested as one flow rather
than as isolated endpoints because the flow is the feature — a signup that works but leaves you with
no project, or an invite that works but grants the wrong organization, is not a partial success.

The security properties pinned here are the ones whose failure is silent: enumeration through
differing error messages, refresh tokens that survive reuse, invitations that transfer by forwarding
a link, and API keys created by someone whose role should not allow it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from proofstep_api.api.dependencies import get_session
from proofstep_api.db.models.identity import Invitation, RefreshToken
from proofstep_api.main import create_app
from proofstep_api.settings import Settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

PASSWORD = "a-sufficiently-long-password"


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(
        Settings(
            env="test",
            jwt_secret="test-secret-value-that-is-long-enough-32",
            # Argon2id is deliberately slow, and these tests hash a lot. The limit is exercised in
            # its own test rather than tripped by every signup here.
            rate_limit_auth_per_min=0,
        )
    )

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


def email() -> str:
    return f"user-{uuid.uuid4().hex[:8]}@example.com"


async def signup(
    client: AsyncClient, address: str | None = None, **extra: object
) -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/signup",
        json={"email": address or email(), "password": PASSWORD, **extra},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def auth(session_payload: dict[str, Any]) -> dict[str, str]:
    return {"authorization": f"Bearer {session_payload['access_token']}"}


class TestSignup:
    async def test_it_creates_a_workspace_not_just_a_user(self, client: AsyncClient) -> None:
        """Sign-up lands you somewhere you can work.

        Every useful first action — send a trace, run a suite — needs a project. An account that
        starts with "create your first workspace" adds a step that exists for the schema's
        convenience, not the person's.
        """
        created = await signup(client)
        assert created["org_id"] is not None
        assert created["project_id"] is not None
        assert created["access_token"]
        assert created["refresh_token"]

        me = (await client.get("/v1/auth/me", headers=auth(created))).json()
        assert me["organizations"][0]["role"] == "owner"

    async def test_the_first_member_is_an_owner(self, client: AsyncClient) -> None:
        # Not admin: the first member is the only one nobody else can lock out, and every later
        # role is granted by them.
        created = await signup(client)
        orgs = (await client.get("/v1/orgs", headers=auth(created))).json()
        assert orgs[0]["role"] == "owner"

    async def test_a_duplicate_email_is_refused(self, client: AsyncClient) -> None:
        address = email()
        await signup(client, address)
        again = await client.post("/v1/auth/signup", json={"email": address, "password": PASSWORD})
        assert again.status_code == 409

    async def test_a_short_password_is_refused_before_hashing(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/auth/signup", json={"email": email(), "password": "short"}
        )
        assert response.status_code == 422

    async def test_the_email_is_normalised(self, client: AsyncClient) -> None:
        # Otherwise `Alice@x.com` and `alice@x.com` are two accounts, and one of them cannot log in
        # with the password they think they set.
        address = email()
        await signup(client, address.upper())
        login = await client.post("/v1/auth/login", json={"email": address, "password": PASSWORD})
        assert login.status_code == 200


class TestLogin:
    async def test_a_wrong_password_and_a_missing_account_look_identical(
        self, client: AsyncClient
    ) -> None:
        """Different messages here turn the login form into an account-enumeration oracle.

        That is how a credential-stuffing list gets refined from "ten million addresses" to "the
        four hundred that have accounts on this product".
        """
        address = email()
        await signup(client, address)

        wrong = await client.post("/v1/auth/login", json={"email": address, "password": "x" * 20})
        missing = await client.post("/v1/auth/login", json={"email": email(), "password": "x" * 20})

        assert wrong.status_code == missing.status_code == 401
        assert wrong.json()["detail"] == missing.json()["detail"]

    async def test_login_returns_a_place_to_land(self, client: AsyncClient) -> None:
        address = email()
        await signup(client, address)
        body = (
            await client.post("/v1/auth/login", json={"email": address, "password": PASSWORD})
        ).json()
        assert body["org_id"]
        assert body["project_id"]


class TestRefreshRotation:
    async def test_a_refresh_returns_a_new_pair(self, client: AsyncClient) -> None:
        created = await signup(client)
        rotated = (
            await client.post("/v1/auth/refresh", json={"refresh_token": created["refresh_token"]})
        ).json()
        assert rotated["refresh_token"] != created["refresh_token"]
        assert rotated["access_token"] != created["access_token"]

    async def test_replaying_a_used_token_revokes_the_whole_family(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """The property that makes rotation worth having.

        Rotation without reuse detection is barely better than a long-lived token: a stolen refresh
        token stays valid until it expires and the theft is invisible, because both parties keep
        working. Revoking the family logs out the thief *and* the victim — the only outcome that
        does not leave someone with a working stolen session.
        """
        created = await signup(client)
        first = created["refresh_token"]
        rotated = (await client.post("/v1/auth/refresh", json={"refresh_token": first})).json()

        replay = await client.post("/v1/auth/refresh", json={"refresh_token": first})
        assert replay.status_code == 401
        assert "already used" in replay.json()["detail"]

        # And the token the legitimate client is holding is dead too.
        after = await client.post(
            "/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
        )
        assert after.status_code == 401

        rows = (await session.execute(select(RefreshToken))).scalars().all()
        assert all(row.revoked_at is not None for row in rows)

    async def test_an_expired_token_is_refused(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        created = await signup(client)
        row = (await session.execute(select(RefreshToken))).scalars().first()
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()

        response = await client.post(
            "/v1/auth/refresh", json={"refresh_token": created["refresh_token"]}
        )
        assert response.status_code == 401

    async def test_logout_ends_the_session(self, client: AsyncClient) -> None:
        created = await signup(client)
        assert (
            await client.post("/v1/auth/logout", json={"refresh_token": created["refresh_token"]})
        ).status_code == 204
        replay = await client.post(
            "/v1/auth/refresh", json={"refresh_token": created["refresh_token"]}
        )
        assert replay.status_code == 401

    async def test_logging_out_an_unknown_token_still_returns_204(
        self, client: AsyncClient
    ) -> None:
        # Reporting "no such token" would let a caller test whether a token is live.
        response = await client.post("/v1/auth/logout", json={"refresh_token": "not-a-token"})
        assert response.status_code == 204


class TestInvitations:
    async def test_a_colleague_can_be_invited_and_join(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        owner = await signup(client)
        colleague_email = email()

        invite = (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/invites",
                headers=auth(owner),
                json={"email": colleague_email, "role": "developer"},
            )
        ).json()
        assert invite["token"], "the token is returned once, at creation"

        colleague = await signup(client, colleague_email)
        joined = (
            await client.post(
                "/v1/invites/accept", headers=auth(colleague), json={"token": invite["token"]}
            )
        ).json()
        assert joined["id"] == owner["org_id"]
        assert joined["role"] == "developer"

        members = (
            await client.get(f"/v1/orgs/{owner['org_id']}/members", headers=auth(owner))
        ).json()
        assert {m["email"] for m in members} == {
            (await client.get("/v1/auth/me", headers=auth(owner))).json()["email"],
            colleague_email,
        }

    async def test_the_token_is_not_stored_in_the_clear(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        # An invitation link grants organization membership. A leaked database must not hand out
        # access to every workspace with an outstanding invite.
        owner = await signup(client)
        invite = (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/invites",
                headers=auth(owner),
                json={"email": email()},
            )
        ).json()

        row = (await session.execute(select(Invitation))).scalars().first()
        assert row is not None
        assert invite["token"].encode() != row.token_hash
        assert len(row.token_hash) == 32

        listed = (
            await client.get(f"/v1/orgs/{owner['org_id']}/invites", headers=auth(owner))
        ).json()
        assert listed[0]["token"] is None, "a listed invite must not reveal its token"

    async def test_a_forwarded_invitation_cannot_be_used_by_someone_else(
        self, client: AsyncClient
    ) -> None:
        """An invitation is addressed, not bearer.

        Otherwise forwarding the link is a transferable membership, and "who did we invite?" stops
        matching "who is in here?" — which is the question asked during an access review.
        """
        owner = await signup(client)
        invite = (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/invites",
                headers=auth(owner),
                json={"email": "intended@example.com"},
            )
        ).json()

        someone_else = await signup(client)
        response = await client.post(
            "/v1/invites/accept", headers=auth(someone_else), json={"token": invite["token"]}
        )
        assert response.status_code == 403

    async def test_an_expired_invitation_is_refused(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        owner = await signup(client)
        colleague_email = email()
        invite = (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/invites",
                headers=auth(owner),
                json={"email": colleague_email},
            )
        ).json()

        row = (await session.execute(select(Invitation))).scalars().first()
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()

        colleague = await signup(client, colleague_email)
        response = await client.post(
            "/v1/invites/accept", headers=auth(colleague), json={"token": invite["token"]}
        )
        assert response.status_code == 404

    async def test_a_token_cannot_be_used_twice(self, client: AsyncClient) -> None:
        owner = await signup(client)
        colleague_email = email()
        invite = (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/invites",
                headers=auth(owner),
                json={"email": colleague_email},
            )
        ).json()
        colleague = await signup(client, colleague_email)
        await client.post(
            "/v1/invites/accept", headers=auth(colleague), json={"token": invite["token"]}
        )
        again = await client.post(
            "/v1/invites/accept", headers=auth(colleague), json={"token": invite["token"]}
        )
        assert again.status_code == 404

    async def test_a_viewer_cannot_invite(self, client: AsyncClient) -> None:
        owner = await signup(client)
        viewer_email = email()
        invite = (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/invites",
                headers=auth(owner),
                json={"email": viewer_email, "role": "viewer"},
            )
        ).json()
        viewer = await signup(client, viewer_email)
        await client.post(
            "/v1/invites/accept", headers=auth(viewer), json={"token": invite["token"]}
        )

        response = await client.post(
            f"/v1/orgs/{owner['org_id']}/invites",
            headers=auth(viewer),
            json={"email": email()},
        )
        assert response.status_code == 403


class TestMembers:
    async def test_an_owner_cannot_be_demoted_or_removed_when_last(
        self, client: AsyncClient
    ) -> None:
        """An organization with no owner is one nobody can administer.

        There is no recovery path from that state that does not involve a database console, so both
        routes that could produce it refuse.
        """
        owner = await signup(client)
        me = (await client.get("/v1/auth/me", headers=auth(owner))).json()

        demote = await client.patch(
            f"/v1/orgs/{owner['org_id']}/members/{me['id']}",
            headers=auth(owner),
            json={"role": "viewer"},
        )
        assert demote.status_code == 403

        remove = await client.delete(
            f"/v1/orgs/{owner['org_id']}/members/{me['id']}", headers=auth(owner)
        )
        assert remove.status_code == 403

    async def test_a_role_can_be_changed_and_takes_effect(self, client: AsyncClient) -> None:
        owner = await signup(client)
        colleague_email = email()
        invite = (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/invites",
                headers=auth(owner),
                json={"email": colleague_email, "role": "viewer"},
            )
        ).json()
        colleague = await signup(client, colleague_email)
        await client.post(
            "/v1/invites/accept", headers=auth(colleague), json={"token": invite["token"]}
        )
        colleague_id = (await client.get("/v1/auth/me", headers=auth(colleague))).json()["id"]

        # A viewer cannot create a project…
        assert (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/projects",
                headers=auth(colleague),
                json={"name": "Nope"},
            )
        ).status_code == 403

        await client.patch(
            f"/v1/orgs/{owner['org_id']}/members/{colleague_id}",
            headers=auth(owner),
            json={"role": "admin"},
        )
        # …and an admin can.
        assert (
            await client.post(
                f"/v1/orgs/{owner['org_id']}/projects",
                headers=auth(colleague),
                json={"name": "Yes"},
            )
        ).status_code == 201

    async def test_another_organization_is_not_found(self, client: AsyncClient) -> None:
        # 404, never 403: a 403 confirms the organization exists, which turns an unguessable id
        # into a confirmed one.
        stranger = await signup(client)
        other = await signup(client)
        response = await client.get(f"/v1/orgs/{other['org_id']}/members", headers=auth(stranger))
        assert response.status_code == 404


class TestApiKeys:
    async def test_a_key_is_created_shown_once_and_works(self, client: AsyncClient) -> None:
        """The whole point of the account layer: a credential a person can put in their app."""
        owner = await signup(client)
        created = (
            await client.post(
                f"/v1/projects/{owner['project_id']}/api-keys",
                headers=auth(owner),
                json={"name": "ci", "scopes": ["ingest", "read"]},
            )
        ).json()
        assert created["token"].startswith("ps_")

        # It authenticates against the rest of the API immediately.
        traces = await client.get(
            "/v1/traces", headers={"authorization": f"Bearer {created['token']}"}
        )
        assert traces.status_code == 200

        listed = (
            await client.get(f"/v1/projects/{owner['project_id']}/api-keys", headers=auth(owner))
        ).json()
        assert listed[0]["token"] is None, "a listed key must never reveal its secret"
        assert listed[0]["prefix"] == created["prefix"]

    async def test_scopes_are_enforced(self, client: AsyncClient) -> None:
        # An ingest-only key that leaks from a container image must not read the traces back.
        owner = await signup(client)
        created = (
            await client.post(
                f"/v1/projects/{owner['project_id']}/api-keys",
                headers=auth(owner),
                json={"scopes": ["ingest"]},
            )
        ).json()
        response = await client.get(
            "/v1/traces", headers={"authorization": f"Bearer {created['token']}"}
        )
        assert response.status_code == 403

    async def test_a_revoked_key_stops_working(self, client: AsyncClient) -> None:
        owner = await signup(client)
        created = (
            await client.post(
                f"/v1/projects/{owner['project_id']}/api-keys",
                headers=auth(owner),
                json={"scopes": ["read"]},
            )
        ).json()
        assert (
            await client.delete(
                f"/v1/projects/{owner['project_id']}/api-keys/{created['id']}",
                headers=auth(owner),
            )
        ).status_code == 204

        response = await client.get(
            "/v1/traces", headers={"authorization": f"Bearer {created['token']}"}
        )
        assert response.status_code == 401

    async def test_an_api_key_cannot_manage_the_account(self, client: AsyncClient) -> None:
        """A machine credential must not be able to add people or mint more credentials.

        Otherwise a leaked ingest key escalates into permanent access: create a user, invite
        yourself, keep a session after the key is rotated.
        """
        owner = await signup(client)
        key = (
            await client.post(
                f"/v1/projects/{owner['project_id']}/api-keys",
                headers=auth(owner),
                json={"scopes": ["read", "write"]},
            )
        ).json()
        head = {"authorization": f"Bearer {key['token']}"}

        assert (await client.get("/v1/orgs", headers=head)).status_code == 403
        assert (
            await client.post("/v1/orgs", headers=head, json={"name": "Sneaky"})
        ).status_code == 403
        assert (
            await client.post(f"/v1/projects/{owner['project_id']}/api-keys", headers=head, json={})
        ).status_code == 403

    async def test_another_organizations_project_is_not_found(self, client: AsyncClient) -> None:
        stranger = await signup(client)
        other = await signup(client)
        response = await client.get(
            f"/v1/projects/{other['project_id']}/api-keys", headers=auth(stranger)
        )
        assert response.status_code == 404


class TestUnderRowLevelSecurity:
    """The same flow, on a connection that row-level security actually applies to.

    Every other test in this file runs as the session superuser, which Postgres exempts from every
    policy unconditionally. That is a configuration no production deployment uses, and the gap is
    not hypothetical: signup created a project and then inserted its default environment with no
    tenant set on the transaction. `environments` has a `WITH CHECK` policy, so the superuser
    accepted the insert and the application role refused it — a 500 on the first request a new user
    ever makes, reachable only after deploying exactly as the hardening guide instructs.

    Two endpoints, because both create a project and then write a tenant-scoped row into it, and
    fixing one would have left the other.
    """

    @pytest_asyncio.fixture
    async def restricted_client(
        self, unprivileged_engine: AsyncEngine
    ) -> AsyncIterator[AsyncClient]:
        app = create_app(
            Settings(
                env="test",
                jwt_secret="test-secret-value-that-is-long-enough-32",
                rate_limit_auth_per_min=0,
            )
        )
        maker = async_sessionmaker(unprivileged_engine, expire_on_commit=False)

        async def override() -> AsyncIterator[AsyncSession]:
            async with maker() as db:
                yield db
                await db.commit()

        app.dependency_overrides[get_session] = override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            yield http

    async def test_signup_creates_its_first_environment(
        self, restricted_client: AsyncClient
    ) -> None:
        created = await signup(restricted_client)
        assert created["project_id"] is not None

    async def test_a_later_project_creates_its_environment_too(
        self, restricted_client: AsyncClient
    ) -> None:
        owner = await signup(restricted_client)
        response = await restricted_client.post(
            f"/v1/orgs/{owner['org_id']}/projects",
            headers=auth(owner),
            json={"name": "Second"},
        )
        assert response.status_code == 201, response.text
