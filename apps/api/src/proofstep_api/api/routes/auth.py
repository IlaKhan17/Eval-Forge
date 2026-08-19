"""Sign-up, sign-in, and session lifetime.

Until now the only credential was a project API key created by a script, which is right for a
machine and impossible for a person: there was no way to become a user, so there was no way for a
team to share anything. This is the layer that turns a self-hosted service into a product someone
can sign up for.

Four decisions, each of which shapes what an attacker can do:

**Sign-up creates an organization and a project, not just a user.** Landing in an empty account with
a "create your first workspace" screen is a step that exists only because the schema needed it.
Every real first action — send a trace, run a suite — needs a project, so sign-up makes one.

**Refresh tokens rotate, and reuse revokes the family.** Each refresh mints a new token and marks
the old one used. Presenting an already-used token means either a client bug or a stolen token, and
the two are indistinguishable from here — so the family is revoked and everyone re-authenticates.
Losing a session is a small cost; a silently cloned session is not.

**Login and sign-up are rate limited by address.** They are the only unauthenticated write paths in
the system, so they are where a password-guessing loop would live. `get_principal` limits everything
behind it, but these run before there is a principal to limit.

**The same answer for "no such user" and "wrong password".** Different messages turn the login form
into an account-enumeration oracle, which is how a credential-stuffing list gets refined.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update

from proofstep_api.api.dependencies import (
    PrincipalDep,
    SessionDep,
    SettingsDep,
    _client_ip,
    _enforce_limit,
)
from proofstep_api.db import rls
from proofstep_api.db.models.identity import (
    Environment,
    Invitation,
    Membership,
    Organization,
    PasswordReset,
    Project,
    RefreshToken,
    User,
)
from proofstep_api.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from proofstep_api.security import keys as key_utils
from proofstep_api.security import passwords, ratelimit, resets
from proofstep_api.security import tokens as token_utils

router = APIRouter(prefix="/v1/auth", tags=["auth"])

#: Where a new account starts. A project and an environment, so the first trace has somewhere to go.
DEFAULT_PROJECT_NAME = "Default"
DEFAULT_ENVIRONMENT = "production"


class SignupIn(BaseModel):
    email: EmailStr
    #: Length is the only rule enforced here. Composition rules ("one symbol, one digit") measurably
    #: push people toward `Password1!` and a manager they do not use; length is what actually costs
    #: an attacker. `passwords.hash_password` rejects anything shorter.
    password: str = Field(min_length=12, max_length=256)
    name: str | None = Field(default=None, max_length=200)
    #: The organization to create. Defaults to the local part of the email so the flow has no
    #: required field a person has to invent before they can try the product.
    organization: str | None = Field(default=None, max_length=200)
    #: An invitation being accepted as part of signing up. When present, this account joins an
    #: existing organization instead of creating one — see the note in `signup`.
    invite_token: str | None = Field(default=None, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str = Field(max_length=512)


class SessionOut(BaseModel):
    access_token: str
    refresh_token: str
    # The OAuth 2 bearer scheme's name, not a secret — S105 sees the word "token" in the field name
    # and the string on the right and assumes the worst.
    token_type: str = "bearer"  # noqa: S105
    expires_at: datetime
    user_id: uuid.UUID
    #: Where the client should land. On sign-up this is the project just created, so the dashboard
    #: never has to guess or make a second round trip to find out where to go.
    org_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None


class MembershipOut(BaseModel):
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    role: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    organizations: list[MembershipOut]


def slugify(value: str, *, fallback: str = "workspace") -> str:
    import re  # noqa: PLC0415 — one call site

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or fallback)[:100]


async def _unique_slug(session: SessionDep, base: str) -> str:
    """A slug nobody else has, without a race that surfaces as a 500.

    Appends a short suffix rather than failing: two people signing up with the same company name is
    an ordinary Tuesday, not an error worth showing either of them.
    """
    candidate = base
    for _ in range(5):
        taken = (
            await session.execute(select(Organization.id).where(Organization.slug == candidate))
        ).scalar_one_or_none()
        if taken is None:
            return candidate
        candidate = f"{base[:92]}-{uuid.uuid4().hex[:6]}"
    return f"{base[:92]}-{uuid.uuid4().hex[:6]}"


async def _issue_session(
    session: SessionDep,
    settings: SettingsDep,
    user: User,
    *,
    org_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    family_id: uuid.UUID | None = None,
) -> SessionOut:
    """Mint an access token and a refresh token for one user.

    `family_id` threads through a rotation: every token descended from one login shares it, which is
    what makes "revoke the family" a meaningful response to a replayed token.
    """
    access, expires_at = token_utils.create_access_token(
        user.id, secret=settings.jwt_secret, ttl_s=settings.access_token_ttl_s
    )
    refresh, digest = token_utils.generate_refresh_token()
    row = RefreshToken(
        user_id=user.id,
        token_hash=digest,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_s),
    )
    if family_id is not None:
        row.family_id = family_id
    session.add(row)
    await session.flush()

    return SessionOut(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        user_id=user.id,
        org_id=org_id,
        project_id=project_id,
    )


async def _limit_by_address(request: Request, settings: SettingsDep) -> None:
    """Rate limit an unauthenticated endpoint by caller address.

    These routes run before there is a principal, so the usual per-credential bucket does not exist
    yet. Without this, sign-up and login are the two unauthenticated write paths in the system and
    the obvious place to point a password-guessing loop.
    """
    await _enforce_limit(
        request, settings, bucket=f"ip:{_client_ip(request)}", klass=ratelimit.AUTH
    )


async def _resolve_invitation(session: SessionDep, token: str, email: str) -> Invitation:
    """The invitation this signup is accepting, or a refusal.

    The email check is the same one `accept_invite` makes, and it matters more here: without it,
    anyone holding a forwarded link could sign up under *their own* address and land inside someone
    else's organization. The invitation names an address; only that address may spend it.
    """
    invitation = (
        await session.execute(
            select(Invitation).where(Invitation.token_hash == key_utils.hash_key(token))
        )
    ).scalar_one_or_none()
    if invitation is None or invitation.accepted_at is not None:
        raise UnauthorizedError("That invitation is not valid.")
    if invitation.expires_at <= datetime.now(UTC):
        raise UnauthorizedError("That invitation has expired. Ask for a new one.")
    if invitation.email.lower() != email:
        raise ForbiddenError("This invitation was sent to a different email address.")
    return invitation


async def _join_by_invitation(
    session: SessionDep, settings: SettingsDep, user: User, invitation: Invitation
) -> SessionOut:
    """Put a brand-new account into the organization that invited it.

    Lands them on a project they can actually see, rather than on the organization with no project
    context — the dashboard's first screen is a trace list, and a trace list with no project is an
    empty state that looks like a broken account.
    """
    organization = await session.get(Organization, invitation.org_id)
    if organization is None or organization.is_deleted:
        raise NotFoundError("That organization no longer exists.")

    session.add(Membership(org_id=invitation.org_id, user_id=user.id, role=invitation.role))
    invitation.accepted_at = datetime.now(UTC)
    await session.flush()

    # The organization's oldest project: the one everyone else is already looking at.
    project_id = (
        await session.execute(
            select(Project.id)
            .where(Project.org_id == organization.id, Project.deleted_at.is_(None))
            .order_by(Project.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()

    return await _issue_session(
        session, settings, user, org_id=organization.id, project_id=project_id
    )


@router.post("/signup", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> SessionOut:
    """Create a user, an organization, a project, and a session — in one call.

    All four, because every useful first action needs a project, and an onboarding flow that makes
    someone create one by hand before they can send a trace is a step that exists for the schema's
    convenience rather than theirs.

    **Unless an invitation came with it.** Somebody invited to an existing workspace does not want a
    workspace of their own; they want the one they were invited to. Creating one anyway and then
    joining leaves every invited user with a permanent empty organization named after their email
    address, cluttering the switcher of everyone who ever accepted an invite. So the token is
    honoured here, in the same transaction: either the account is created *and* joins, or neither
    happens and the invitation is still there to try again.
    """
    await _limit_by_address(request, settings)

    email = body.email.lower().strip()
    existing = (
        await session.execute(select(User.id).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        # A distinct message here is an account-enumeration oracle. It is accepted on *sign-up*
        # rather than login, because a signup form that silently succeeded for an existing address
        # would strand the person with no way to understand why they cannot log in.
        raise ConflictError("An account with that email already exists.")

    invitation = None
    if body.invite_token is not None:
        # Resolved before the account is created, so an invitation that turns out to be expired
        # fails the whole request rather than leaving a half-finished signup behind.
        invitation = await _resolve_invitation(session, body.invite_token, email)

    user = User(email=email, password_hash=passwords.hash_password(body.password), name=body.name)
    session.add(user)
    await session.flush()

    if invitation is not None:
        return await _join_by_invitation(session, settings, user, invitation)

    org_name = body.organization or email.split("@")[0]
    organization = Organization(name=org_name, slug=await _unique_slug(session, slugify(org_name)))
    session.add(organization)
    await session.flush()

    # Owner, not admin. The first member of an organization is the only one who cannot be locked out
    # of it by someone else, and every later role is granted by them.
    session.add(Membership(org_id=organization.id, user_id=user.id, role="owner"))

    project = Project(
        org_id=organization.id, name=DEFAULT_PROJECT_NAME, slug=slugify(DEFAULT_PROJECT_NAME)
    )
    session.add(project)
    await session.flush()
    # The transaction has no tenant: the request that created this account was unauthenticated, so
    # the dependency had no project to scope it to. `environments` is tenant-scoped and its policy
    # has a WITH CHECK, so inserting into it with no tenant set is refused outright.
    #
    # Invisible on a superuser connection, which bypasses every policy, and fatal on the
    # unprivileged role a production deployment is supposed to use. So: signup worked in
    # development and returned a 500 in production, on the first request a new user ever makes.
    await rls.set_tenant(session, project.id)
    session.add(Environment(project_id=project.id, name=DEFAULT_ENVIRONMENT))
    await session.flush()

    return await _issue_session(
        session, settings, user, org_id=organization.id, project_id=project.id
    )


@router.post("/login", response_model=SessionOut)
async def login(
    body: LoginIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> SessionOut:
    await _limit_by_address(request, settings)

    email = body.email.lower().strip()
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()

    # Verify against a dummy hash when the user does not exist, so a missing account and a wrong
    # password take the same time. Otherwise response latency enumerates who has an account here —
    # the same reasoning as the API-key lookup in `dependencies._principal_from_api_key`.
    stored = user.password_hash if user and user.password_hash else passwords.DUMMY_HASH
    matched = passwords.verify_password(body.password, stored)

    if user is None or not matched or not user.is_active:
        raise UnauthorizedError("That email and password do not match an account.")

    user.last_login_at = datetime.now(UTC)

    membership = (
        (
            await session.execute(
                select(Membership)
                .where(Membership.user_id == user.id)
                .order_by(Membership.created_at)
            )
        )
        .scalars()
        .first()
    )
    project = None
    if membership is not None:
        project = (
            (
                await session.execute(
                    select(Project)
                    .where(Project.org_id == membership.org_id, Project.deleted_at.is_(None))
                    .order_by(Project.created_at)
                )
            )
            .scalars()
            .first()
        )

    return await _issue_session(
        session,
        settings,
        user,
        org_id=membership.org_id if membership else None,
        project_id=project.id if project else None,
    )


@router.post("/refresh", response_model=SessionOut)
async def refresh(
    body: RefreshIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> SessionOut:
    """Exchange a refresh token for a new pair, and detect a replayed one.

    Rotation without reuse detection is barely better than a long-lived token: a stolen refresh
    token stays valid until it expires, and the theft is invisible because both parties keep
    working. Here, a token presented twice revokes every token in its family — the legitimate client
    and the attacker both get logged out, which is the only outcome that does not leave a thief with
    a working session.
    """
    await _limit_by_address(request, settings)

    digest = token_utils.hash_refresh_token(body.refresh_token)
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()
    if row is None:
        raise UnauthorizedError("That session has expired. Sign in again.")

    now = datetime.now(UTC)
    if row.used_at is not None or row.revoked_at is not None:
        await _revoke_family(session, row.family_id, now=now)
        raise UnauthorizedError(
            "That session token was already used. Every session in this family has been signed "
            "out as a precaution; sign in again."
        )
    if row.expires_at <= now:
        raise UnauthorizedError("That session has expired. Sign in again.")

    row.used_at = now
    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("That session is no longer valid.")

    membership = (
        (
            await session.execute(
                select(Membership)
                .where(Membership.user_id == user.id)
                .order_by(Membership.created_at)
            )
        )
        .scalars()
        .first()
    )
    return await _issue_session(
        session,
        settings,
        user,
        org_id=membership.org_id if membership else None,
        family_id=row.family_id,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshIn, session: SessionDep) -> Response:
    """End a session.

    Revokes the whole family rather than the one token: a person clicking "sign out" means this
    device is done, and leaving its descendants valid would make the button a lie.

    Unauthenticated on purpose — signing out with an expired access token has to work, and the
    refresh token itself is the proof of possession.
    """
    digest = token_utils.hash_refresh_token(body.refresh_token)
    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()
    if row is not None:
        await _revoke_family(session, row.family_id, now=datetime.now(UTC))
    # 204 either way. Reporting "no such token" would let a caller test whether a token is live.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _revoke_family(session: SessionDep, family_id: uuid.UUID, *, now: datetime) -> None:
    rows = (
        (
            await session.execute(
                select(RefreshToken).where(
                    RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.revoked_at = now
    await session.flush()


@router.get("/me", response_model=MeOut)
async def me(principal: PrincipalDep, session: SessionDep) -> MeOut:
    """The signed-in user and the organizations they belong to.

    The dashboard's first call after login: it decides what the workspace switcher contains.
    """
    if principal.kind != "user":
        # An API key identifies a project, not a person. Returning something plausible here would
        # invite a client to treat a machine credential as a session.
        raise ForbiddenError("This endpoint is for signed-in users, not API keys.")

    user = await session.get(User, uuid.UUID(principal.id))
    if user is None:
        raise UnauthorizedError("That session is no longer valid.")

    rows = (
        await session.execute(
            select(Membership, Organization)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user.id, Organization.deleted_at.is_(None))
            .order_by(Membership.created_at)
        )
    ).all()

    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        organizations=[
            MembershipOut(org_id=org.id, org_name=org.name, org_slug=org.slug, role=membership.role)
            for membership, org in rows
        ],
    )


# ------------------------------------------------------------------ forgotten passwords
#
# The flow that exists because the alternative is "email the operator and hope". Without it, a
# forgotten password is a permanently lost account, which for a product with more than one user is
# not a gap so much as a guarantee of support tickets.
#
# Three properties hold this together, and each is a decision that has to survive the next edit.
#
# **The response never carries the token.** It is the whole ballgame. An endpoint that returns the
# reset link to whoever asked for it is not a password reset, it is a password bypass: anyone could
# post someone else's address and be handed a working account-takeover credential. The token leaves
# this process by exactly one route — `resets.deliver` — which writes it where only somebody with
# server access can read it.
#
# **The answer is the same whether or not the account exists.** Same status, same body, same
# approximate timing. A different response for a known address turns this into a membership oracle
# against any email list.
#
# **Using a reset invalidates every session.** A password is reset either because it was forgotten
# or because it was stolen, and the second case is the one worth designing for: leaving the
# attacker's refresh token alive means the reset changed nothing for them.


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(max_length=128)
    password: str = Field(min_length=12, max_length=256)


class AcknowledgedOut(BaseModel):
    """Deliberately contentless.

    Anything specific here — "we sent it", "no such account" — is the enumeration oracle this
    endpoint exists to avoid. The message is the same one the page shows either way.
    """

    detail: str = (
        "If that address has an account, a reset link is on its way. "
        "Check with whoever runs this installation if it does not arrive."
    )


@router.post("/forgot", response_model=AcknowledgedOut, status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    body: ForgotIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> AcknowledgedOut:
    """Begin a password reset. Always accepted, whether or not the address is known."""
    await _limit_by_address(request, settings)
    email = body.email.lower().strip()

    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is not None:
        token, digest = resets.generate()
        session.add(
            PasswordReset(
                user_id=user.id,
                token_hash=digest,
                expires_at=datetime.now(UTC) + timedelta(seconds=settings.password_reset_ttl_s),
                requested_by_ip=_client_ip(request),
            )
        )
        await session.flush()
        await resets.deliver(user.email, token, settings=settings)

    # No `else`. Not even a log line distinguishing the two — an operator reading logs is not the
    # threat, but a log that says "reset requested for an unknown address" is one grep away from
    # being the oracle this endpoint refuses to be over HTTP.
    return AcknowledgedOut()


@router.post("/reset", response_model=AcknowledgedOut)
async def reset_password(
    body: ResetIn,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> AcknowledgedOut:
    """Finish a password reset, and sign every session out.

    Unlike `/forgot`, this one *does* refuse a bad token — there is nothing to enumerate, because
    the token was random and is not an account identifier.
    """
    await _limit_by_address(request, settings)

    digest = resets.hash_token(body.token)
    reset = (
        await session.execute(select(PasswordReset).where(PasswordReset.token_hash == digest))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if reset is None or reset.used_at is not None:
        raise UnauthorizedError("That reset link is not valid. Ask for a new one.")
    if reset.expires_at <= now:
        raise UnauthorizedError("That reset link has expired. Ask for a new one.")

    user = await session.get(User, reset.user_id)
    if user is None:
        # The cascade should have taken the reset row with the account. Belt and braces: a token
        # that resolves to nobody must not resolve to somebody else later.
        raise UnauthorizedError("That reset link is not valid. Ask for a new one.")

    user.password_hash = passwords.hash_password(body.password)
    reset.used_at = now

    # Every *other* outstanding reset for this user, too. Two links in a mailbox and only one spent
    # leaves the second one live, which is the same standing credential this flow just closed.
    await session.execute(
        update(PasswordReset)
        .where(
            PasswordReset.user_id == user.id,
            PasswordReset.used_at.is_(None),
        )
        .values(used_at=now)
    )

    # And every session. See the note at the top of this section: if the password was reset because
    # it was stolen, an attacker's refresh token outliving the reset makes the reset ornamental.
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.flush()

    # No session issued. Signing the caller straight in would be friendlier and would also mean a
    # single stolen link is a session without ever proving the new password works. Log in.
    return AcknowledgedOut(detail="Your password has been changed. Sign in with it.")
