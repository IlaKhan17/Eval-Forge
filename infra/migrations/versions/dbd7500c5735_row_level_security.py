"""row level security

The database-layer backstop for tenant isolation (docs/SECURITY.md §4, ADR-015). Deferred to this
phase deliberately: the ubiquitous `project_id` column is what makes it a one-migration change, and
deferring it was safe *because* the repository predicate and the cross-tenant test suite provide the
coverage in the meantime.

Two things this migration does that are easy to leave out and consequential:

- `FORCE ROW LEVEL SECURITY`, so the table owner is not exempt. A self-hosted deployment that runs
  migrations and the application as one role would otherwise have RLS enabled and doing nothing,
  which is worse than not having it — it looks protected.
- `WITH CHECK` as well as `USING`, so writes are refused and not merely reads filtered. `USING`
  alone lets a caller insert a row into another tenant that it then cannot see.

Policies are applied to the partitioned parents *and* their existing partitions. A policy on the
parent does not propagate to partitions attached before it was created, and `traces`/`spans`/
`span_events` already have a DEFAULT partition and monthly children from earlier migrations.

Revision ID: dbd7500c5735
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from evalforge_api.db.rls import (
    POLICY_SUFFIX,
    PROTECTED_TABLES,
    drop_statements,
    policy_statements,
)
from sqlalchemy import text

revision: str = "dbd7500c5735"
down_revision: str | None = "235fa23ea98f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> list[str]:
    """Protected tables, plus any partitions already attached to them.

    A partition inherits the parent's policy only if it is attached *after* the policy exists, so
    the children created by earlier migrations need the DDL applied directly. Discovered from the
    catalogue rather than listed, because the set of monthly partitions depends on when the
    migration runs.
    """
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
    for table in _tables():
        for statement in policy_statements(table):
            op.execute(statement)


def downgrade() -> None:
    """Drop every policy this migration could have created, discovered from the catalogue.

    Discovered rather than derived from `PROTECTED_TABLES`, because that list is code and code
    changes: moving a table to the exemption list after this migration ran would make the downgrade
    skip it and leave the policy behind. That happened once, and the symptom was every request
    returning 401 — `api_keys` kept a tenant policy while being the table authentication reads
    *before* a tenant is known.
    """
    connection = op.get_bind()
    # The suffix is bound rather than interpolated. It is a module constant and could not carry user
    # input, but a DDL migration is the last place to leave string-built SQL.
    orphans = connection.execute(
        text(
            "SELECT c.relname FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND p.polname LIKE :pattern"
        ),
        {"pattern": f"%\\_{POLICY_SUFFIX}"},
    ).scalars()
    for table in sorted({str(name) for name in orphans}):
        for statement in drop_statements(table):
            op.execute(statement)
