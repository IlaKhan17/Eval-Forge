"""worker heartbeats

One row per worker, updated in place, so "is the background work happening?" is answerable from the
database rather than from whether anyone happened to be watching the logs.

See `evalforge_api.db.models.ops.WorkerHeartbeat` for why this is a table and not a Redis key.

Revision ID: c7d3e91a5b42
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d3e91a5b42"
down_revision: str | None = "a41c9b6f2d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("worker_name", sa.String(length=100), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "detail", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_heartbeats")),
        # The upsert target. Without it the worker inserts a new row per beat and the table grows
        # by 1440 rows a day to answer a question about the newest one.
        sa.UniqueConstraint("worker_name", name="uq_worker_heartbeats_worker_name"),
    )

    # Same guarded grant as worker_dead_letters: ALTER DEFAULT PRIVILEGES only covers tables created
    # by the role that ran create_app_role.sql, and a deployment that migrates as a different role
    # would otherwise get a worker that cannot record it is alive.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalforge_app') THEN
                GRANT SELECT, INSERT, UPDATE ON worker_heartbeats TO evalforge_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
