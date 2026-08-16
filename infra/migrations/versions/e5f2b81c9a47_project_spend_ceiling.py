"""project spend ceiling

A monthly limit on server-initiated spend, plus the decision reason that records an evaluation
skipped because of it.

See `proofstep_api.services.budget` for what the ceiling can and cannot stop — only spend the server
initiates, which is online evaluation.

Revision ID: e5f2b81c9a47
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f2b81c9a47"
down_revision: str | None = "c7d3e91a5b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The reason set, before and after. Spelled out rather than derived from the model, because a
#: migration that reads today's code cannot be replayed against tomorrow's.
OLD_REASONS = "('deterministic', 'sampled', 'escalated', 'forced', 'not_sampled', 'capped')"
NEW_REASONS = (
    "('deterministic', 'sampled', 'escalated', 'forced', 'not_sampled', 'capped', 'budget')"
)


def upgrade() -> None:
    op.add_column(
        "projects",
        # NULL means unlimited, which is deliberately distinct from 0 — 0 is a real setting for a
        # project that should run only its free deterministic rules.
        sa.Column("monthly_cost_limit", sa.Numeric(12, 4), nullable=True),
    )

    # Raw SQL rather than op.drop_constraint/create_check_constraint. Those run the metadata naming
    # convention over the name they are given, so passing the constraint's real name produces
    # `ck_online_evaluations_ck_online_evaluations_decision_re_456e` and the drop fails on a
    # constraint that does not exist. A CHECK cannot be altered in place, so it is dropped and
    # recreated with the wider set.
    op.execute(
        "ALTER TABLE online_evaluations DROP CONSTRAINT ck_online_evaluations_decision_reason_valid"
    )
    op.execute(
        "ALTER TABLE online_evaluations ADD CONSTRAINT "
        "ck_online_evaluations_decision_reason_valid "
        f"CHECK (decision_reason IN {NEW_REASONS})"
    )


def downgrade() -> None:
    # Rows recorded under the new reason would violate the old constraint, so they are rewritten to
    # the closest older meaning rather than left to break the downgrade. `capped` is that: both say
    # "a limit stopped this", and the distinction they lose is exactly the one this migration added.
    op.execute(
        "UPDATE online_evaluations SET decision_reason = 'capped' WHERE decision_reason = 'budget'"
    )
    op.execute(
        "ALTER TABLE online_evaluations DROP CONSTRAINT ck_online_evaluations_decision_reason_valid"
    )
    op.execute(
        "ALTER TABLE online_evaluations ADD CONSTRAINT "
        "ck_online_evaluations_decision_reason_valid "
        f"CHECK (decision_reason IN {OLD_REASONS})"
    )
    op.drop_column("projects", "monthly_cost_limit")
