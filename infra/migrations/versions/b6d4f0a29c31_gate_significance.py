"""gate significance and power

Two fields on a quality gate rule: the alpha for a paired significance test, and whether an
underpowered gate should report ERROR rather than a green tick.

Stored rather than derived, because a gate set is the durable record of what a repository asked for.
A rule that came back from the database without them would gate differently from the one that was
published — the exact divergence apps/api/tests/test_parity.py exists to catch, and it caught these
before they shipped.

Revision ID: b6d4f0a29c31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d4f0a29c31"
down_revision: str | None = "f8a3c21d7e59"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("quality_gate_rules", sa.Column("significance", sa.Float(), nullable=True))
    op.add_column(
        "quality_gate_rules",
        # Server default as well as a Python default: existing rows predate the column, and a NULL
        # here would make `require_power` neither true nor false for every gate ever published.
        sa.Column("require_power", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("quality_gate_rules", "require_power")
    op.drop_column("quality_gate_rules", "significance")
