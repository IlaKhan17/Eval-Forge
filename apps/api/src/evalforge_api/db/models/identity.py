"""Identity and tenancy: organizations, users, projects, keys, audit.

Every tenant-scoped table carries `project_id` (and `org_id` where useful) even
where it could be reached by a join. That denormalization is deliberate: it makes
every index tenant-prefixed, and it makes a future row-level-security policy a
one-migration change rather than a schema redesign (ADR-015).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from evalforge_api.db.base import (
    IdentifiedBase,
    SoftDeleteMixin,
    TimestampMixin,
    UpdatedAtMixin,
    uuid7,
)

ROLES = ("owner", "admin", "developer", "reviewer", "viewer")
CAPTURE_MODES = ("full", "redacted", "metadata_only", "disabled")
SCOPES = ("ingest", "read", "write", "annotate")


class Organization(IdentifiedBase, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))

    projects: Mapped[list[Project]] = relationship(back_populates="organization")

    __table_args__ = (
        Index(
            "uq_organizations_slug_active",
            "slug",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )


class User(IdentifiedBase, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "users"

    # CITEXT would be better but needs an extension; lowercasing on write achieves
    # the same thing, which matters because case-variant duplicate accounts are a
    # real account-takeover vector.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, default=None)
    name: Mapped[str | None] = mapped_column(String(200), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Membership(IdentifiedBase, TimestampMixin):
    __tablename__ = "memberships"

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))

    __table_args__ = (
        UniqueConstraint("org_id", "user_id"),
        CheckConstraint(f"role IN {ROLES}", name="role_valid"),
        Index("ix_memberships_user_id", "user_id"),
    )


class Project(IdentifiedBase, TimestampMixin, UpdatedAtMixin, SoftDeleteMixin):
    __tablename__ = "projects"

    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Safe defaults are a security control, not a preference: capture is redacted
    # and retention is short, because data never stored cannot leak.
    default_capture_mode: Mapped[str] = mapped_column(String(20), default="redacted")
    retention_days_traces: Mapped[int] = mapped_column(Integer, default=30)
    retention_days_payloads: Mapped[int] = mapped_column(Integer, default=14)
    online_eval_sample_rate: Mapped[float] = mapped_column(default=0.01)

    organization: Mapped[Organization] = relationship(back_populates="projects")

    __table_args__ = (
        Index(
            "uq_projects_org_slug_active",
            "org_id",
            "slug",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
        CheckConstraint(f"default_capture_mode IN {CAPTURE_MODES}", name="capture_mode_valid"),
        CheckConstraint(
            "online_eval_sample_rate >= 0 AND online_eval_sample_rate <= 1",
            name="sample_rate_ratio",
        ),
    )


class Environment(IdentifiedBase, TimestampMixin):
    """A table rather than a free-text span attribute.

    Retention, sampling, and gates all differ per environment, and a typo'd string
    would silently create a fourth environment nobody is watching.
    """

    __tablename__ = "environments"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(50))

    __table_args__ = (UniqueConstraint("project_id", "name"),)


class ApiKey(IdentifiedBase, TimestampMixin):
    __tablename__ = "api_keys"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL"), default=None
    )
    name: Mapped[str] = mapped_column(String(200), default="default")

    # The public handle is indexed and stored in the clear; only the SHA-256 of the
    # full token is retained, so a database dump yields nothing usable.
    prefix: Mapped[str] = mapped_column(String(64), unique=True)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32))

    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(20)), default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        Index(
            "ix_api_keys_project_active",
            "project_id",
            postgresql_where="revoked_at IS NULL",
        ),
    )

    def is_usable(self, *, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= now)


class AuditLog(IdentifiedBase, TimestampMixin):
    """Append-only. The application role holds no UPDATE or DELETE grant.

    An audit trail the application can rewrite is not an audit trail.
    """

    __tablename__ = "audit_logs"

    org_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    project_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(100), default=None)
    action: Mapped[str] = mapped_column(String(100))
    resource_type: Mapped[str | None] = mapped_column(String(50), default=None)
    resource_id: Mapped[str | None] = mapped_column(String(100), default=None)
    audit_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(INET, default=None)
    user_agent: Mapped[str | None] = mapped_column(String(500), default=None)

    __table_args__ = (
        CheckConstraint("actor_type IN ('user', 'api_key', 'system')", name="actor_type_valid"),
        Index("ix_audit_logs_org_id_created_at", "org_id", "created_at"),
        Index("ix_audit_logs_project_id_resource", "project_id", "resource_type", "resource_id"),
    )


class RefreshToken(IdentifiedBase, TimestampMixin):
    """Rotating refresh tokens with reuse detection.

    Each use issues a new token and marks the old one used. If a *used* token is
    presented again, the whole family is revoked: that pattern means the token was
    stolen and replayed, and the safe response is to end every session it belongs to.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID] = mapped_column(default=uuid7)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
    )
