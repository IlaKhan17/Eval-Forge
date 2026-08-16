#!/usr/bin/env python
"""Create or update the unprivileged application role, and re-apply its grants.

Run immediately after `alembic upgrade head`, as the owning role. `infra/docker/entrypoint.sh`
does exactly that, so a containerised deploy needs no manual step.

**Why this runs on every deploy and not once at install.** The grants in `create_app_role.sql`
include `GRANT ... ON ALL TABLES`, which is a snapshot of the tables that exist at the moment it
runs. `ALTER DEFAULT PRIVILEGES` covers tables created *afterwards by the same role*, which is
usually the migration role and therefore usually enough — but "usually" is doing real work in that
sentence. A deploy that migrates as a different owner, or a migration that creates a table via a raw
`op.execute`, lands a table the application cannot read. The symptom is a permission error on one
endpoint after a deploy that looked clean, and re-running the grants costs milliseconds.

**Why it executes the .sql file rather than reimplementing it.** The privilege set *is* the security
boundary. Two copies of it — one for humans with psql, one for the container — would agree on the
day they were written and diverge at the first change, and the divergence would be silent in both
directions: a grant the container adds that the documented path does not, or the reverse.
"""

from __future__ import annotations

import sys
from pathlib import Path

from proofstep_api.settings import get_settings
from sqlalchemy import create_engine, text

SQL_FILE = Path(__file__).resolve().parent / "create_app_role.sql"


def main() -> int:
    settings = get_settings()
    # Same guard as the migration path: this script issues DDL and role changes, so it is one of the
    # two places that genuinely needs the owning credential.
    settings.require_separate_migration_role()

    password = settings.postgres_password
    if not password:
        # Refusing beats proceeding. Executing the file without a password would raise inside the DO
        # block anyway, but the message here can say which setting is missing.
        print(
            "POSTGRES_PASSWORD (or POSTGRES_PASSWORD_FILE) is empty — that is the password the "
            "application role is being given, so there is nothing to provision.",
            file=sys.stderr,
        )
        return 1

    statements = SQL_FILE.read_text()

    # The sync driver, deliberately: this is a one-shot script run before the application
    # starts, and an event loop would add a moving part to a step whose whole job is to be boring.
    engine = create_engine(settings.migration_url, future=True)
    try:
        with engine.begin() as conn:
            # `set_config(..., is_local => true)` rather than `SET LOCAL`: SET takes no bind
            # parameters, so the alternative is interpolating a password into a statement string.
            # Local scope means the value dies with the transaction instead of lingering in a pooled
            # session for the next user of that connection to read back with `current_setting`.
            conn.execute(
                text("SELECT set_config('proofstep.role_password', :pw, true)"), {"pw": password}
            )
            conn.exec_driver_sql(statements)
    finally:
        engine.dispose()

    print("application role provisioned: grants re-applied and password set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
