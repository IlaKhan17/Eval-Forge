#!/usr/bin/env python
"""Check a deployment before it takes traffic.

    uv run python scripts/preflight.py            # human-readable
    uv run python scripts/preflight.py --json     # for a deploy pipeline

Exits 0 when every check passes, 1 when any *blocking* check fails, and prints advisories either
way. Intended as a deploy gate: run it after migrations and before routing traffic.

Every check here exists because its failure is **silent in production**. A missing partition
surfaces as a rejected ingest hours later; a role that bypasses row-level security looks exactly
like one that does not; an unrotated bootstrap key works perfectly until someone finds it in a
shell history. None of these show up in a smoke test that fetches `/healthz`.

Deliberately not included: anything that requires guessing at the deployment's shape. Whether TLS
terminates upstream, whether backups exist, and whether alerts route anywhere cannot be determined
from inside the process, and a check that guesses would either be noise or false comfort. Those are
in `docs/OPERATIONS.md` as a human checklist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from evalforge_api.db.partitions import missing_partitions
from evalforge_api.db.rls import PROTECTED_TABLES, role_bypasses_rls, verify_enforced
from evalforge_api.settings import Settings, get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

#: Bootstrap keys are created by a development script and printed to a terminal. One still working
#: in production means a credential exists whose only record is somebody's scrollback.
DEV_KEY_PREFIXES = ("ef_dev_", "ef_test_")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "blocking": self.blocking,
            "detail": self.detail,
        }


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail, blocking=blocking))

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and check.blocking]

    @property
    def advisories(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and not check.blocking]


async def run_checks(settings: Settings) -> Report:
    report = Report()
    _check_settings(settings, report)

    engine = create_async_engine(settings.sqlalchemy_url)
    try:
        async with engine.connect() as connection:
            await _check_database(connection, settings, report)
    except Exception as exc:  # a database that cannot be reached is the first thing to report
        report.add("database", ok=False, detail=f"could not connect: {type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()

    return report


def _check_settings(settings: Settings, report: Report) -> None:
    report.add(
        "environment",
        ok=settings.is_production,
        detail=f"ENV={settings.env}",
        # Advisory: running this against staging is a legitimate use, and failing on it would make
        # the tool useless exactly where rehearsing a deploy matters most.
        blocking=False,
    )
    report.add(
        "migration role is separate",
        ok=settings.migration_database_url is not None,
        detail=(
            "migrations run as a distinct role"
            if settings.migration_database_url
            else "MIGRATION_DATABASE_URL is unset, so the application owns its tables and is "
            "exempt from its own policies unless FORCE is set on every one"
        ),
    )
    report.add(
        "object storage configured",
        ok=bool(settings.s3_endpoint),
        detail=(
            f"payloads offload to {settings.s3_endpoint}"
            if settings.s3_endpoint
            else "no S3 endpoint: large payloads stay inline, which works but bloats the row store"
        ),
        blocking=False,
    )
    report.add(
        "debug off",
        ok=not settings.debug,
        detail="debug is on" if settings.debug else "debug is off",
    )
    report.add(
        "CORS is not a wildcard",
        ok="*" not in settings.cors_origins,
        detail=f"cors_origins={settings.cors_origins or '[]'}",
    )


async def _check_database(connection: Any, settings: Settings, report: Report) -> None:
    role, bypasses, reason = await role_bypasses_rls(connection)
    report.add(
        "row-level security applies to this role",
        ok=not bypasses or settings.allow_rls_bypass,
        detail=(
            f"connected as {role!r}; {reason}"
            if bypasses
            else f"connected as {role!r}, subject to every policy"
        )
        + (" (ALLOW_RLS_BYPASS is set)" if bypasses and settings.allow_rls_bypass else ""),
    )

    state = await verify_enforced(connection)
    report.add(
        "tenant policies present",
        ok=not state["problems"],
        detail=(
            f"{len(state['enforced'])}/{len(PROTECTED_TABLES)} protected tables enforced"
            if not state["problems"]
            else "; ".join(state["problems"][:3])
        ),
    )

    missing = await missing_partitions(connection)
    report.add(
        "partitions cover the current month",
        ok=not missing,
        detail=("all covered" if not missing else f"missing: {', '.join(missing)}"),
    )

    version = (
        await connection.execute(text("SELECT version_num FROM alembic_version"))
    ).scalar_one_or_none()
    report.add(
        "schema is migrated",
        ok=version is not None,
        detail=f"alembic at {version}" if version else "no alembic_version row",
    )

    # Development-issued keys, still live. `bootstrap_dev.py` refuses to run against production, so
    # one of these existing means the database was promoted from a development install — which is a
    # normal thing to do and a credential nobody is tracking.
    dev_keys = (
        await connection.execute(
            text(
                "SELECT count(*) FROM api_keys WHERE revoked_at IS NULL "
                "AND (prefix LIKE 'ef_dev_%' OR prefix LIKE 'ef_test_%')"
            )
        )
    ).scalar_one()
    report.add(
        "no development keys are live",
        ok=dev_keys == 0,
        detail=(
            "none"
            if dev_keys == 0
            else f"{dev_keys} key(s) issued by a development bootstrap are still valid; "
            "rotate them with scripts/manage_keys.py"
        ),
    )

    unresolved = (
        await connection.execute(
            text("SELECT count(*) FROM worker_dead_letters WHERE resolved_at IS NULL")
        )
    ).scalar_one()
    report.add(
        "no unresolved job failures",
        ok=unresolved == 0,
        detail=f"{unresolved} unresolved dead letter(s)" if unresolved else "none",
        # Advisory: a dead letter from last week should not block today's deploy, but nobody should
        # deploy without knowing it is there.
        blocking=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    settings = get_settings()
    report = asyncio.run(run_checks(settings))

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not report.failed,
                    "checks": [check.as_dict() for check in report.checks],
                },
                indent=2,
            )
        )
    else:
        for check in report.checks:
            mark = "✓" if check.ok else ("✗" if check.blocking else "!")
            print(f"  {mark} {check.name:<40} {check.detail}")
        print()
        if report.failed:
            print(f"{len(report.failed)} blocking check(s) failed. See docs/OPERATIONS.md.")
        elif report.advisories:
            print(f"ready, with {len(report.advisories)} advisory note(s).")
        else:
            print("ready.")

    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
