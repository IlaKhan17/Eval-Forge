#!/usr/bin/env python
"""Create, list, rotate, and revoke API keys — including in production.

    uv run python scripts/manage_keys.py list --project acme
    uv run python scripts/manage_keys.py create --project acme --name ci --scopes ingest read
    uv run python scripts/manage_keys.py rotate --prefix ef_prod_ab12… --grace-hours 24
    uv run python scripts/manage_keys.py revoke --prefix ef_prod_ab12…

`bootstrap_dev.py` deliberately refuses to run against production, which left no way to issue a
credential in the environment that most needs one managed properly. This is that way.

Three properties worth stating, because each is a decision:

- **Rotation is overlap, not replacement.** `rotate` mints the new key and schedules the old one to
  expire after a grace period rather than revoking it immediately. A rotation that breaks every
  running job the moment it happens is a rotation nobody performs, and a credential nobody rotates
  is worse than a slightly longer overlap.
- **The token is shown exactly once.** Only a SHA-256 of it is stored, so a lost key is reissued
  rather than recovered. That is also why `list` shows prefixes: enough to identify a key, useless
  for authenticating as one.
- **Revocation is immediate but not instant.** The API caches key lookups for
  `api_key_cache_ttl_s` (30s by default), so a revoked key can still work for that long. Said out
  loud here because "I revoked it and it still works" is otherwise a frightening thirty seconds.

Every action is written to the audit log, which is the record that survives this shell session.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from evalforge_api.db.models.identity import ApiKey, AuditLog, Environment, Project
from evalforge_api.security.keys import generate
from evalforge_api.settings import get_settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

#: Long enough that a scheduled job on a daily cadence rotates cleanly; short enough that a
#: forgotten rotation does not leave two live credentials for a week.
DEFAULT_GRACE_HOURS = 24

#: The scopes a key may hold. Mirrors the permission tiers the API enforces; keeping the list here
#: means an operator issuing a credential is offered exactly what the server understands.
SCOPES = ("ingest", "read", "write", "annotate")


async def _session(url: str) -> tuple[Any, Any]:
    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _project(session: Any, slug: str) -> Project:
    project = (
        await session.execute(
            select(Project).where(Project.slug == slug, Project.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if project is None:
        msg = f"no project with slug {slug!r}"
        raise SystemExit(msg)
    return project


async def _audit(session: Any, *, action: str, project_id: Any, detail: dict[str, Any]) -> None:
    """Record what was done.

    The reason this is not optional: key management is the one operation whose *effects* are
    invisible afterwards. A key that exists tells you nothing about who issued it or why, and the
    person asking is usually asking during an incident.
    """
    session.add(
        AuditLog(
            project_id=project_id,
            # `system`, because the schema's actor types are user / api_key / system and an
            # operator at a shell is none of the first two. The audit row's value is the action and
            # the prefix; pretending to identify the human would be worse than not claiming to.
            actor_type="system",
            action=action,
            resource_type="api_key",
            resource_id=str(detail.get("prefix", "")),
            audit_metadata=detail,
        )
    )


async def cmd_list(args: argparse.Namespace, session: Any) -> int:
    project = await _project(session, args.project)
    rows = (
        (
            await session.execute(
                select(ApiKey)
                .where(ApiKey.project_id == project.id)
                .order_by(ApiKey.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        print("no keys")
        return 0

    now = datetime.now(UTC)
    print(f"{'prefix':<32} {'name':<16} {'scopes':<28} status")
    for row in rows:
        if row.revoked_at:
            status = f"revoked {row.revoked_at:%Y-%m-%d}"
        elif row.expires_at and row.expires_at <= now:
            status = f"expired {row.expires_at:%Y-%m-%d}"
        elif row.expires_at:
            status = f"expires {row.expires_at:%Y-%m-%d %H:%M}"
        else:
            status = "active"
        used = f" · last used {row.last_used_at:%Y-%m-%d}" if row.last_used_at else " · never used"
        print(f"{row.prefix:<32} {row.name:<16} {','.join(row.scopes):<28} {status}{used}")
    return 0


async def cmd_create(args: argparse.Namespace, session: Any) -> int:
    project = await _project(session, args.project)
    environment = (
        await session.execute(
            select(Environment).where(
                Environment.project_id == project.id, Environment.name == args.env
            )
        )
    ).scalar_one_or_none()

    generated = generate(args.env)
    expires_at = (
        datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None
    )
    session.add(
        ApiKey(
            project_id=project.id,
            environment_id=environment.id if environment else None,
            name=args.name,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=list(args.scopes),
            expires_at=expires_at,
        )
    )
    await _audit(
        session,
        action="api_key.create",
        project_id=project.id,
        detail={"prefix": generated.prefix, "name": args.name, "scopes": list(args.scopes)},
    )
    await session.commit()

    print(f"\nAPI key (shown once, not recoverable):\n\n  {generated.token}\n")
    if expires_at:
        print(f"expires {expires_at:%Y-%m-%d %H:%M} UTC")
    return 0


async def cmd_rotate(args: argparse.Namespace, session: Any) -> int:
    old = (
        await session.execute(select(ApiKey).where(ApiKey.prefix == args.prefix))
    ).scalar_one_or_none()
    if old is None:
        print(f"no key with prefix {args.prefix!r}", file=sys.stderr)
        return 1
    if old.revoked_at:
        print(f"{args.prefix} is already revoked; create a new key instead", file=sys.stderr)
        return 1

    environment = (
        await session.execute(select(Environment).where(Environment.id == old.environment_id))
    ).scalar_one_or_none()
    env_name = environment.name if environment else "prod"

    generated = generate(env_name)
    session.add(
        ApiKey(
            project_id=old.project_id,
            environment_id=old.environment_id,
            name=old.name,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=list(old.scopes),
            expires_at=old.expires_at,
        )
    )

    # Expiry, not revocation. The old key keeps working through the grace window so that whatever
    # is using it can be updated without an outage — which is the difference between a rotation
    # procedure that gets followed and one that gets postponed indefinitely.
    cutoff = datetime.now(UTC) + timedelta(hours=args.grace_hours)
    old.expires_at = cutoff

    await _audit(
        session,
        action="api_key.rotate",
        project_id=old.project_id,
        detail={
            "prefix": generated.prefix,
            "replaces": old.prefix,
            "old_expires_at": cutoff.isoformat(),
        },
    )
    await session.commit()

    print(f"\nNew API key (shown once, not recoverable):\n\n  {generated.token}\n")
    print(f"the previous key {old.prefix} keeps working until {cutoff:%Y-%m-%d %H:%M} UTC")
    print("update every consumer, then run:")
    print(f"  uv run python scripts/manage_keys.py revoke --prefix {old.prefix}")
    return 0


async def cmd_revoke(args: argparse.Namespace, session: Any) -> int:
    key = (
        await session.execute(select(ApiKey).where(ApiKey.prefix == args.prefix))
    ).scalar_one_or_none()
    if key is None:
        print(f"no key with prefix {args.prefix!r}", file=sys.stderr)
        return 1
    if key.revoked_at:
        print(f"{args.prefix} was already revoked at {key.revoked_at:%Y-%m-%d %H:%M} UTC")
        return 0

    key.revoked_at = datetime.now(UTC)
    await _audit(
        session,
        action="api_key.revoke",
        project_id=key.project_id,
        detail={"prefix": key.prefix, "reason": args.reason},
    )
    await session.commit()

    ttl = get_settings().api_key_cache_ttl_s
    print(f"revoked {key.prefix}")
    print(f"in-flight requests may still authenticate for up to {ttl}s (the key cache TTL)")
    return 0


COMMANDS = {
    "list": cmd_list,
    "create": cmd_create,
    "rotate": cmd_rotate,
    "revoke": cmd_revoke,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="Show every key for a project")
    listing.add_argument("--project", required=True)

    create = sub.add_parser("create", help="Issue a new key")
    create.add_argument("--project", required=True)
    create.add_argument("--name", default="default")
    create.add_argument("--env", default="prod")
    create.add_argument("--scopes", nargs="+", default=["ingest", "read"], choices=SCOPES)
    create.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="Expire after N days. Recommended for anything a human holds.",
    )

    rotate = sub.add_parser("rotate", help="Issue a replacement and expire the old key")
    rotate.add_argument("--prefix", required=True)
    rotate.add_argument("--grace-hours", type=int, default=DEFAULT_GRACE_HOURS)

    revoke = sub.add_parser("revoke", help="Revoke a key immediately")
    revoke.add_argument("--prefix", required=True)
    revoke.add_argument("--reason", default="")

    return parser


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine, maker = await _session(settings.sqlalchemy_url)
    try:
        async with maker() as session:
            return int(await COMMANDS[args.command](args, session))
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
