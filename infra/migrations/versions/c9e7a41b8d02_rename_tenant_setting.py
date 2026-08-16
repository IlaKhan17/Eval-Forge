"""rename the tenant setting to proofstep.project_id

The row-level-security policies embed the name of the per-transaction setting they read. Renaming
the product renamed the constant in the code; this re-applies every policy so the database agrees.

Getting this wrong is silent in the worst way: the policies would still exist and still be enforced,
but they would read a setting nothing sets, and `nullif(current_setting(..., true), '')::uuid`
returns NULL for a missing setting — so every query would match nothing. Failing closed rather than
open, which is the right direction, but the symptom would be an application that returns empty
results for every tenant.

Both parents and existing partitions, because a policy on a partitioned parent does not propagate
to partitions attached before it was created.

Revision ID: c9e7a41b8d02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from proofstep_api.db.rls import PROTECTED_TABLES, policy_statements
from sqlalchemy import text

revision: str = "c9e7a41b8d02"
down_revision: str | None = "b6d4f0a29c31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_SETTING = "evalforge.project_id"


def _tables() -> list[str]:
    connection = op.get_bind()
    existing = {
        str(name)
        for name in connection.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')"
            )
        ).scalars()
    }
    tables = [table for table in PROTECTED_TABLES if table in existing]
    for parent in PROTECTED_TABLES:
        tables.extend(
            sorted(name for name in existing if name.startswith(f"{parent}_") and name != parent)
        )
    return tables


def upgrade() -> None:
    # `policy_statements` reads the current constant, so this rewrites each policy with the new
    # setting name. It drops and recreates rather than altering: a policy's expression cannot be
    # changed in place.
    for table in _tables():
        for statement in policy_statements(table):
            op.execute(statement)


def downgrade() -> None:
    predicate = f"project_id = nullif(current_setting('{OLD_SETTING}', true), '')::uuid"
    for table in _tables():
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
