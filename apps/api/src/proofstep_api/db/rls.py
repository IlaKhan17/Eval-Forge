"""Row-level security: the database-layer backstop for tenant isolation.

Three layers guard against cross-tenant access (docs/SECURITY.md §4, ADR-015):

1. the repository layer injects `project_id` into every query
2. a parameterized cross-tenant test suite covers every endpoint
3. **this** — Postgres RLS policies keyed off a per-transaction setting

RLS is the backstop for a bug in layer 1, not a substitute for it. That framing decides how it is
built: a policy that the application can accidentally satisfy is not a backstop, so the setting it
reads is established once per transaction from the authenticated principal and never from anything
a request body can influence.

## Why `current_setting`, and why per transaction

`SET LOCAL` scopes the value to the current transaction, so a pooled connection cannot leak a
tenant into the next request that borrows it. A plain `SET` would persist for the connection's
lifetime, which under a connection pool is precisely the cross-tenant bug RLS is meant to catch.

The predicate is `project_id = nullif(current_setting('proofstep.project_id', true), '')::uuid`.
Both parts of that are load-bearing. The `true` flag makes a *missing* setting return NULL instead
of raising; `nullif(..., '')` handles the *cleared* setting, because clearing one leaves an empty
string and `''::uuid` raises. Together they mean a connection with no tenant context sees nothing
rather than erroring — a code path that forgets to bind the tenant fails closed, and returns an
empty result instead of a 500.

## Why the application role is not the owner

Postgres exempts a table's owner from its RLS policies unless `FORCE ROW LEVEL SECURITY` is set,
and superusers are always exempt. A deployment where the application connects as the owner has RLS
switched off without anyone noticing. `verify_enforced` exists so that misconfiguration is
detectable rather than silent, and `proofstep doctor` reports it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

#: The per-transaction setting the policies read. Namespaced, because an unqualified custom GUC is
#: rejected by Postgres.
TENANT_SETTING = "proofstep.project_id"

#: Tables the policies cover: every table carrying `project_id`. Enumerated rather than discovered
#: at runtime so that adding a tenant-scoped table without a policy is a *migration* mismatch the
#: drift check catches, not a silent hole.
PROTECTED_TABLES: tuple[str, ...] = (
    "aggregate_metrics",
    "annotations",
    "audit_logs",
    "dataset_examples",
    "dataset_versions",
    "datasets",
    "environments",
    "evaluation_results",
    "evaluator_calibrations",
    "evaluator_versions",
    "evaluators",
    "experiment_results",
    "experiment_runs",
    "experiments",
    "online_eval_rules",
    "online_evaluations",
    "payload_objects",
    "quality_gate_rules",
    "quality_gate_sets",
    "review_assignments",
    "review_queues",
    "span_events",
    "spans",
    "traces",
    "trajectory_policies",
    "trajectory_policy_versions",
)

#: Tables deliberately left out, and why. Kept as data so the omissions are reviewable rather than
#: implicit in the absence of a name from the list above.
UNPROTECTED_TABLES: dict[str, str] = {
    # The bootstrapping exception, and the only interesting one. Authentication has to read this
    # table to *discover* which tenant a credential belongs to, so a policy keyed on the tenant
    # would make every request fail: the lookup happens before a project is known. Isolation here
    # comes from the lookup itself — a globally unique `prefix` and a SHA-256 of the secret, so a
    # row is useless without the token that produced it. Layer 1 still filters by project on every
    # management endpoint.
    "api_keys": "read before the tenant is known; a tenant policy would break authentication",
    "organizations": "no project_id; scoped by membership, and needed to resolve a project at all",
    "projects": "the tenant itself — a policy here would make the tenant unresolvable",
    "users": "identities span organizations; membership is what scopes them",
    "memberships": "the join that decides scope; filtered by org at the repository layer",
    # Org-scoped, and read *before* the invitee is a member of anything — accepting an invitation is
    # precisely the moment someone has no membership to filter by. Isolation comes from the token:
    # the lookup is by a unique SHA-256 digest, so a row is useless without the link that produced
    # it, and acceptance additionally requires the signed-in email to match.
    "invitations": "read before the invitee is a member; scoped by a hashed single-use token",
    "refresh_tokens": "keyed by user, not project; rotation must work before a tenant is known",
    "alembic_version": "migration bookkeeping",
    # Deployment-level operational records, not tenant data. These jobs sweep every project, so a
    # failure belongs to the installation; giving the row a project_id would mean either inventing
    # one or writing a row per project for a single failure. Nothing tenant-identifying is stored —
    # see db/models/ops.py, which also explains why no traceback is kept.
    "worker_dead_letters": "operational records for jobs that span every project, not tenant data",
    # Liveness for the worker process, which serves every project. One row per worker, holding a
    # name and a timestamp — nothing tenant-identifying, and a tenant policy would hide a worker's
    # heartbeat from the very endpoint that exists to report it is missing.
    "worker_heartbeats": "process liveness for a worker that serves every project",
}

POLICY_SUFFIX = "tenant_isolation"


def policy_statements(table: str) -> list[str]:
    """DDL enabling RLS on one table.

    `FORCE` is included deliberately. Without it the table's owner bypasses the policy, and a
    self-hosted deployment that runs migrations and the application as the same role would have RLS
    enabled and doing nothing — the worst of both worlds, because it would appear protected.

    The policy is `USING` *and* `WITH CHECK`: reads are filtered and writes are refused. A policy
    with only `USING` lets a caller insert a row into another tenant that it then cannot see, which
    is a data-corruption bug wearing a security bug's clothes.
    """
    # `nullif(..., '')` matters and is easy to omit. The `true` flag makes a *missing* setting
    # return NULL, but clearing one sets it to the empty string — and `''::uuid` raises
    # "invalid input syntax", so a connection with no tenant context would get a 500 rather than an
    # empty result. That is the opposite of failing closed, and it is how the first version behaved.
    predicate = f"project_id = nullif(current_setting('{TENANT_SETTING}', true), '')::uuid"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_{POLICY_SUFFIX} ON {table}",
        f"CREATE POLICY {table}_{POLICY_SUFFIX} ON {table} "
        f"USING ({predicate}) WITH CHECK ({predicate})",
    ]


def drop_statements(table: str) -> list[str]:
    return [
        f"DROP POLICY IF EXISTS {table}_{POLICY_SUFFIX} ON {table}",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]


async def apply_policies(
    connection: AsyncConnection, tables: Sequence[str] = PROTECTED_TABLES
) -> None:
    for table in tables:
        for statement in policy_statements(table):
            await connection.execute(text(statement))


async def set_tenant(session: AsyncSession, project_id: uuid.UUID | None) -> None:
    """Establish the tenant for the current transaction.

    `SET LOCAL`, so the value dies with the transaction and cannot follow a pooled connection into
    the next request. Called from the request dependency once the principal is known, and never
    from anything a request body can reach.

    A `None` project clears the setting rather than skipping the call. Skipping would leave whatever
    the previous transaction set on this connection — and while `SET LOCAL` already prevents that,
    relying on it silently would make the guarantee depend on a subtlety instead of a statement.
    """
    if project_id is None:
        await session.execute(text(f"SET LOCAL {TENANT_SETTING} = ''"))
        return
    # Bound as a parameter, not interpolated: this value originates from a credential lookup, but a
    # SET statement built by string concatenation is one refactor away from being reachable.
    await session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :project_id, true)"),
        {"project_id": str(project_id)},
    )


async def current_tenant(session: AsyncSession) -> uuid.UUID | None:
    raw = (
        await session.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)"))
    ).scalar_one_or_none()
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


async def role_bypasses_rls(connection: AsyncConnection) -> tuple[str, bool, str]:
    """Whether the connected role is exempt from every policy.

    The single most important check in this module, and the one that is easiest to omit. Postgres
    exempts superusers and roles with `BYPASSRLS` from RLS unconditionally — `FORCE ROW LEVEL
    SECURITY` does not reach them. So an application that connects as the database superuser has
    RLS installed, enabled, forced, policied, and *completely inert*, with nothing in the
    application's behaviour to suggest it.

    This is not hypothetical: the default `docker compose` role in this repository is a superuser,
    so the first run of the policies above changed nothing at all. The migration was correct and the
    protection was zero.
    """
    row = (
        await connection.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        )
    ).one_or_none()
    if row is None:
        return "unknown", False, "could not resolve the current role"

    role, is_super, bypasses = str(row[0]), bool(row[1]), bool(row[2])
    if is_super:
        return role, True, f"role {role!r} is a superuser, which is exempt from every RLS policy"
    if bypasses:
        return role, True, f"role {role!r} has BYPASSRLS, which is exempt from every RLS policy"
    return role, False, ""


async def verify_enforced(connection: AsyncConnection) -> dict[str, list[str]]:
    """Report which protected tables actually have RLS enforced.

    Exists because every way RLS silently stops working is invisible from the application: the
    policy can be missing, `FORCE` can be off while the app connects as the table owner, the
    connected role can be exempt, or a new tenant-scoped table can be added without a policy. A
    security control whose failure mode is "everything keeps working" needs an explicit check.
    """
    rows = (
        await connection.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "  (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                # `'p'` as well as `'r'`: traces, spans, and span_events are partitioned parents,
                # which report as 'p' and would otherwise look absent from their own schema.
                "WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')"
            )
        )
    ).all()
    state = {
        str(name): (bool(enabled), bool(forced), int(policies))
        for name, enabled, forced, policies in rows
    }

    enforced: list[str] = []
    problems: list[str] = []
    for table in PROTECTED_TABLES:
        enabled, forced, policies = state.get(table, (False, False, 0))
        if enabled and forced and policies:
            enforced.append(table)
        elif table not in state:
            # A partitioned parent reports separately from its children; a missing table here means
            # the migration did not run, which is worth distinguishing from a missing policy.
            problems.append(f"{table}: not present in this schema")
        else:
            problems.append(f"{table}: enabled={enabled} forced={forced} policies={policies}")

    # A tenant-scoped table with no policy is the hole this function exists to find.
    unlisted = [
        table
        for table in state
        if table not in PROTECTED_TABLES
        and table not in UNPROTECTED_TABLES
        and not any(table.startswith(f"{parent}_") for parent in PROTECTED_TABLES)
    ]
    # A policy on a table we deliberately excused is as much a problem as a missing one: it means
    # something is filtering rows nobody expects it to. This found a real orphan — `api_keys` kept a
    # policy after being moved to the exemption list, because the migration's downgrade derived its
    # table list from code that had already changed. The symptom was every request returning 401.
    for table, why in UNPROTECTED_TABLES.items():
        enabled, _forced, policies = state.get(table, (False, False, 0))
        if enabled or policies:
            problems.append(f"{table}: has an unexpected RLS policy, but is excused because {why}")

    role, bypasses, reason = await role_bypasses_rls(connection)
    if bypasses:
        # Listed first, because it makes every other line in this report irrelevant. A table can be
        # enabled, forced, and policied and still return another tenant's rows to this role.
        problems.insert(0, f"RLS IS NOT IN EFFECT: {reason}")

    return {
        "enforced": enforced,
        "problems": problems,
        "unreviewed": sorted(unlisted),
        "role": [role],
    }


__all__ = [
    "PROTECTED_TABLES",
    "TENANT_SETTING",
    "UNPROTECTED_TABLES",
    "apply_policies",
    "current_tenant",
    "drop_statements",
    "policy_statements",
    "role_bypasses_rls",
    "set_tenant",
    "verify_enforced",
]
