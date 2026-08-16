"""Organizations, members, projects, and API keys — the self-service surface.

Everything here was previously a shell script an operator ran. That is the difference between a
service you host for yourself and a product other people use: a team has to be able to add a
colleague, create a project, and mint a key for their CI without anyone opening a terminal on the
server.

The shape of authorisation here, because it is easy to get subtly wrong:

- **Membership is on the organization, not the project.** A person joins an org with a role, and
  that role applies to every project in it. Per-project roles are a real need eventually, and
  deliberately not now — the schema supports adding them without a migration to memberships, and
  inventing them early would double the number of places an access check has to be right.
- **The role gate is `Permission.MEMBERS_MANAGE` / `KEYS_MANAGE`, not "is admin".** Roles map to
  permissions in one table (`security/permissions.py`); checking the permission means a future role
  slots in without revisiting every route.
- **Cross-organization access is 404, never 403.** A 403 confirms the organization exists, which
  turns an unguessable id into a confirmed one. Same rule the project routes already follow.

Invitations are by email and expire. They carry a single-use token that is *hashed* at rest, for the
same reason API keys are: an invitation link is a credential that grants membership, and a leaked
database should not hand out organization access.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from proofstep_api.api.dependencies import PrincipalDep, SessionDep
from proofstep_api.api.routes.auth import slugify
from proofstep_api.db import rls
from proofstep_api.db.models.identity import (
    ApiKey,
    Environment,
    Invitation,
    Membership,
    Organization,
    Project,
    User,
)
from proofstep_api.errors import ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from proofstep_api.security import keys as key_utils
from proofstep_api.security.permissions import Permission, permissions_for_role

router = APIRouter(prefix="/v1", tags=["accounts"])

#: How long an invitation link stays valid. Long enough to survive a weekend and a spam folder,
#: short enough that a link forwarded into a public channel a month ago is dead.
INVITE_TTL_DAYS = 14

ASSIGNABLE_ROLES = ("admin", "developer", "reviewer", "viewer")
#: `owner` is deliberately not assignable through the API. Ownership transfer is a distinct action
#: with its own confirmation, and folding it into "change role" makes accidental self-demotion —
#: the one mistake nobody can undo — a single dropdown away.


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    slug: str


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str | None
    role: str
    joined_at: datetime


class InviteIn(BaseModel):
    email: EmailStr
    role: str = "developer"


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    expires_at: datetime
    #: Returned exactly once, at creation. Stored only as a hash, so a link that is lost is reissued
    #: rather than recovered — the same contract as an API key.
    token: str | None = None


class AcceptIn(BaseModel):
    token: str = Field(max_length=128)


class RoleIn(BaseModel):
    role: str


class ApiKeyIn(BaseModel):
    name: str = Field(default="default", max_length=200)
    scopes: list[str] = Field(default_factory=lambda: ["ingest", "read"])
    environment: str | None = Field(default=None, max_length=50)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    #: Shown once, at creation, and never again.
    token: str | None = None


async def _user(principal: PrincipalDep) -> uuid.UUID:
    """The signed-in user, or a refusal.

    These routes are for people. An API key identifies a project and has no business creating
    organizations or inviting members — a leaked ingest key must not be able to add an account.
    """
    if principal.kind != "user":
        raise ForbiddenError("This endpoint is for signed-in users, not API keys.")
    return uuid.UUID(principal.id)


UserId = Annotated[uuid.UUID, Depends(_user)]


async def _membership(session: SessionDep, org_id: uuid.UUID, user_id: uuid.UUID) -> Membership:
    row = (
        await session.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        # 404 rather than 403: a 403 confirms the organization exists.
        raise NotFoundError("No such organization.")
    return row


async def _require(
    session: SessionDep, org_id: uuid.UUID, user_id: uuid.UUID, permission: Permission
) -> Membership:
    membership = await _membership(session, org_id, user_id)
    if permission not in permissions_for_role(membership.role):
        raise ForbiddenError(f"Your role ({membership.role}) cannot perform this action.")
    return membership


# ------------------------------------------------------------------ organizations


@router.post("/orgs", response_model=OrgOut, status_code=status.HTTP_201_CREATED)
async def create_org(body: OrgIn, session: SessionDep, user_id: UserId) -> OrgOut:
    """A second organization for an existing user — an agency, a side project, a separate client."""
    from proofstep_api.api.routes.auth import _unique_slug  # noqa: PLC0415 — avoids a cycle

    organization = Organization(
        name=body.name, slug=await _unique_slug(session, slugify(body.name))
    )
    session.add(organization)
    await session.flush()
    session.add(Membership(org_id=organization.id, user_id=user_id, role="owner"))
    await session.flush()
    return OrgOut(id=organization.id, name=organization.name, slug=organization.slug, role="owner")


@router.get("/orgs", response_model=list[OrgOut])
async def list_orgs(session: SessionDep, user_id: UserId) -> list[OrgOut]:
    rows = (
        await session.execute(
            select(Membership, Organization)
            .join(Organization, Organization.id == Membership.org_id)
            .where(Membership.user_id == user_id, Organization.deleted_at.is_(None))
            .order_by(Membership.created_at)
        )
    ).all()
    return [
        OrgOut(id=org.id, name=org.name, slug=org.slug, role=membership.role)
        for membership, org in rows
    ]


# ------------------------------------------------------------------------ members


@router.get("/orgs/{org_id}/members", response_model=list[MemberOut])
async def list_members(org_id: uuid.UUID, session: SessionDep, user_id: UserId) -> list[MemberOut]:
    # Any member may see who else is in the organization. Hiding colleagues from each other would
    # make "who can see our traces?" unanswerable from inside the product.
    await _membership(session, org_id, user_id)
    rows = (
        await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == org_id)
            .order_by(Membership.created_at)
        )
    ).all()
    return [
        MemberOut(
            user_id=member.id,
            email=member.email,
            name=member.name,
            role=membership.role,
            joined_at=membership.created_at,
        )
        for membership, member in rows
    ]


@router.post(
    "/orgs/{org_id}/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED
)
async def invite_member(
    org_id: uuid.UUID, body: InviteIn, session: SessionDep, user_id: UserId
) -> InviteOut:
    await _require(session, org_id, user_id, Permission.MEMBERS_MANAGE)
    if body.role not in ASSIGNABLE_ROLES:
        raise ConflictError(f"Role must be one of {', '.join(ASSIGNABLE_ROLES)}.")

    email = body.email.lower().strip()
    existing_user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing_user is not None:
        already = (
            await session.execute(
                select(Membership.id).where(
                    Membership.org_id == org_id, Membership.user_id == existing_user.id
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            raise ConflictError("That person is already a member.")

    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        org_id=org_id,
        email=email,
        role=body.role,
        token_hash=key_utils.hash_key(token),
        invited_by=user_id,
        expires_at=datetime.now(UTC) + timedelta(days=INVITE_TTL_DAYS),
    )
    session.add(invitation)
    await session.flush()

    # The token comes back once. Delivery is the caller's problem for now — a self-hosted install
    # has no mail server, and inventing one here would make email a hard dependency of running the
    # product at all. The cloud deployment sends it; the API returns it either way.
    return InviteOut(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        token=token,
    )


@router.get("/orgs/{org_id}/invites", response_model=list[InviteOut])
async def list_invites(org_id: uuid.UUID, session: SessionDep, user_id: UserId) -> list[InviteOut]:
    await _require(session, org_id, user_id, Permission.MEMBERS_MANAGE)
    rows = (
        (
            await session.execute(
                select(Invitation)
                .where(Invitation.org_id == org_id, Invitation.accepted_at.is_(None))
                .order_by(Invitation.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    # Without tokens: they are hashed at rest and cannot be recovered, which is the point.
    return [
        InviteOut(id=row.id, email=row.email, role=row.role, expires_at=row.expires_at)
        for row in rows
    ]


@router.post("/invites/accept", response_model=OrgOut)
async def accept_invite(body: AcceptIn, session: SessionDep, user_id: UserId) -> OrgOut:
    """Join an organization with an invitation token.

    The signed-in user's email must match the address the invitation was sent to. Otherwise a
    forwarded link is a transferable membership, and "who did we invite?" stops matching "who is in
    here?".
    """
    digest = key_utils.hash_key(body.token)
    invitation = (
        await session.execute(select(Invitation).where(Invitation.token_hash == digest))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if invitation is None or invitation.accepted_at is not None:
        raise NotFoundError("That invitation is not valid.")
    if invitation.expires_at <= now:
        raise NotFoundError("That invitation has expired. Ask for a new one.")

    user = await session.get(User, user_id)
    if user is None:
        raise UnauthorizedError("That session is no longer valid.")
    if user.email.lower() != invitation.email.lower():
        raise ForbiddenError("This invitation was sent to a different email address.")

    organization = await session.get(Organization, invitation.org_id)
    if organization is None or organization.is_deleted:
        raise NotFoundError("That organization no longer exists.")

    existing = (
        await session.execute(
            select(Membership).where(
                Membership.org_id == invitation.org_id, Membership.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(Membership(org_id=invitation.org_id, user_id=user_id, role=invitation.role))
    invitation.accepted_at = now
    await session.flush()

    return OrgOut(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        role=existing.role if existing else invitation.role,
    )


@router.patch("/orgs/{org_id}/members/{member_id}", response_model=MemberOut)
async def change_role(
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    body: RoleIn,
    session: SessionDep,
    user_id: UserId,
) -> MemberOut:
    await _require(session, org_id, user_id, Permission.MEMBERS_MANAGE)
    if body.role not in ASSIGNABLE_ROLES:
        raise ConflictError(f"Role must be one of {', '.join(ASSIGNABLE_ROLES)}.")

    membership = await _membership(session, org_id, member_id)
    if membership.role == "owner":
        # The one change that can leave an organization with nobody able to administer it.
        raise ForbiddenError("An owner's role cannot be changed through this endpoint.")

    membership.role = body.role
    await session.flush()
    member = await session.get(User, member_id)
    assert member is not None
    return MemberOut(
        user_id=member.id,
        email=member.email,
        name=member.name,
        role=membership.role,
        joined_at=membership.created_at,
    )


@router.delete("/orgs/{org_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    org_id: uuid.UUID, member_id: uuid.UUID, session: SessionDep, user_id: UserId
) -> Response:
    await _require(session, org_id, user_id, Permission.MEMBERS_MANAGE)
    membership = await _membership(session, org_id, member_id)

    if membership.role == "owner":
        owners = (
            (
                await session.execute(
                    select(Membership).where(
                        Membership.org_id == org_id, Membership.role == "owner"
                    )
                )
            )
            .scalars()
            .all()
        )
        # An organization with no owner is one nobody can administer, invite into, or delete — a
        # state with no recovery path that does not involve a database console.
        if len(owners) <= 1:
            raise ForbiddenError("An organization must keep at least one owner.")

    await session.delete(membership)
    await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------- projects


@router.post("/orgs/{org_id}/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    org_id: uuid.UUID, body: ProjectIn, session: SessionDep, user_id: UserId
) -> ProjectOut:
    await _require(session, org_id, user_id, Permission.PROJECT_MANAGE)

    slug = slugify(body.name)
    clash = (
        await session.execute(
            select(Project.id).where(
                Project.org_id == org_id, Project.slug == slug, Project.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise ConflictError(f"A project with slug {slug!r} already exists in this organization.")

    project = Project(org_id=org_id, name=body.name, slug=slug)
    session.add(project)
    await session.flush()
    # Scope the transaction to the project that was just created. The caller's session is scoped to
    # whichever project their credential named — or to none — and `environments` has a WITH CHECK
    # policy, so the insert below is refused under any other tenant. Same failure as in signup, and
    # equally invisible to a connection that bypasses RLS.
    #
    # Safe to move the tenant here because nothing afterwards reads another project's rows: the
    # handler returns the project it just made.
    await rls.set_tenant(session, project.id)
    # An environment, so the first trace has somewhere to land without another setup step.
    session.add(Environment(project_id=project.id, name="production"))
    await session.flush()
    return ProjectOut(id=project.id, org_id=org_id, name=project.name, slug=project.slug)


@router.get("/orgs/{org_id}/projects", response_model=list[ProjectOut])
async def list_projects(
    org_id: uuid.UUID, session: SessionDep, user_id: UserId
) -> list[ProjectOut]:
    await _membership(session, org_id, user_id)
    rows = (
        (
            await session.execute(
                select(Project)
                .where(Project.org_id == org_id, Project.deleted_at.is_(None))
                .order_by(Project.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [ProjectOut(id=row.id, org_id=row.org_id, name=row.name, slug=row.slug) for row in rows]


# ----------------------------------------------------------------------- api keys


async def _project_for_user(
    session: SessionDep, project_id: uuid.UUID, user_id: uuid.UUID, permission: Permission
) -> Project:
    project = await session.get(Project, project_id)
    if project is None or project.is_deleted:
        raise NotFoundError("No such project.")
    await _require(session, project.org_id, user_id, permission)
    return project


@router.get("/projects/{project_id}/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    project_id: uuid.UUID,
    session: SessionDep,
    user_id: UserId,
    include_revoked: Annotated[bool, Query()] = False,
) -> list[ApiKeyOut]:
    await _project_for_user(session, project_id, user_id, Permission.KEYS_MANAGE)
    query = select(ApiKey).where(ApiKey.project_id == project_id)
    if not include_revoked:
        query = query.where(ApiKey.revoked_at.is_(None))

    rows = (await session.execute(query.order_by(ApiKey.created_at.desc()))).scalars().all()
    return [
        ApiKeyOut(
            id=row.id,
            name=row.name,
            # The prefix, never the key. It is enough to identify which credential is which in a
            # list, and useless for authenticating as one.
            prefix=row.prefix,
            scopes=list(row.scopes),
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


@router.post("/projects/{project_id}/api-keys", response_model=ApiKeyOut, status_code=201)
async def create_api_key(
    project_id: uuid.UUID, body: ApiKeyIn, session: SessionDep, user_id: UserId
) -> ApiKeyOut:
    """Mint a key. The token is returned once and stored only as a SHA-256 digest."""
    project = await _project_for_user(session, project_id, user_id, Permission.KEYS_MANAGE)

    unknown = set(body.scopes) - {"ingest", "read", "write", "annotate"}
    if unknown:
        raise ConflictError(f"Unknown scopes: {', '.join(sorted(unknown))}.")
    if not body.scopes:
        raise ConflictError("A key with no scopes cannot do anything; pick at least one.")

    environment_name = body.environment or "production"
    environment = (
        await session.execute(
            select(Environment).where(
                Environment.project_id == project.id, Environment.name == environment_name
            )
        )
    ).scalar_one_or_none()

    generated = key_utils.generate(environment_name)
    row = ApiKey(
        project_id=project.id,
        environment_id=environment.id if environment else None,
        name=body.name,
        prefix=generated.prefix,
        key_hash=generated.key_hash,
        scopes=list(body.scopes),
        created_by=user_id,
        expires_at=(
            datetime.now(UTC) + timedelta(days=body.expires_days) if body.expires_days else None
        ),
    )
    session.add(row)
    await session.flush()

    return ApiKeyOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes),
        created_at=row.created_at,
        last_used_at=None,
        expires_at=row.expires_at,
        revoked_at=None,
        token=generated.token,
    )


@router.delete("/projects/{project_id}/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    project_id: uuid.UUID, key_id: uuid.UUID, session: SessionDep, user_id: UserId
) -> Response:
    await _project_for_user(session, project_id, user_id, Permission.KEYS_MANAGE)
    row = await session.get(ApiKey, key_id)
    if row is None or row.project_id != project_id:
        raise NotFoundError("No such API key.")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.flush()
    # Revocation takes effect within the key cache TTL — see docs/OPERATIONS.md.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
