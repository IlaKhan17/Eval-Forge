"""password resets

Pending password resets. See `proofstep_api.db.models.identity.PasswordReset` for why the token is
hashed at rest and why the row is single-use.

Written by hand, like the rest of these: autogenerate does not know which indexes exist for which
query, and a reviewer cannot tell an intentional index from an accidental one in generated output.

Revision ID: d4b8e0f37a91
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4b8e0f37a91"
down_revision: str | None = "c9e7a41b8d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "password_resets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Unique so one token can never address two rows, and hashed so reading this table gives an
        # attacker nothing: a reset link is the one credential that takes over an account without
        # the password.
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Single use. A link that stays valid until it expires is a standing account-takeover
        # credential sitting in a mailbox.
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_by_ip", postgresql.INET(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_password_resets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_resets")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_resets_token_hash")),
    )
    # "The outstanding requests for this user": the query behind invalidating the others when one is
    # used, and behind refusing to mint an unbounded number of them.
    op.create_index(
        "ix_password_resets_user_id_created_at", "password_resets", ["user_id", "created_at"]
    )

    # Same guarded grant as the ops tables. ALTER DEFAULT PRIVILEGES only covers tables created by
    # the role that ran create_app_role.sql, and a deployment that migrates as a different owner
    # would otherwise get a password-reset flow that fails at the insert.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'proofstep_app') THEN
                GRANT SELECT, INSERT, UPDATE ON password_resets TO proofstep_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_password_resets_user_id_created_at", table_name="password_resets")
    op.drop_table("password_resets")
