"""invitations

Pending invitations to join an organization. See `evalforge_api.db.models.identity.Invitation` for
why the token is hashed at rest.

Revision ID: f8a3c21d7e59
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8a3c21d7e59"
down_revision: str | None = "e5f2b81c9a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        # Unique so a token cannot be reused across rows, and hashed so the table is useless to
        # anyone who reads it — an invitation link grants organization membership.
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_invitations_org_id_organizations"),
            ondelete="CASCADE",
        ),
        # SET NULL, not CASCADE: deleting the person who sent an invitation must not delete the
        # record that it happened.
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name=op.f("fk_invitations_invited_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
        sa.CheckConstraint(
            "role IN ('owner', 'admin', 'developer', 'reviewer', 'viewer')",
            name=op.f("ck_invitations_role_valid"),
        ),
    )
    op.create_index("ix_invitations_org_id_email", "invitations", ["org_id", "email"])

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalforge_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON invitations TO evalforge_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_invitations_org_id_email", table_name="invitations")
    op.drop_table("invitations")
