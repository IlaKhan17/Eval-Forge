#!/usr/bin/env python
"""Create a local organization, project, environment, and API key.

For development only. Everything the dashboard and the SDK need to talk to a local
API has to exist in the database first, and until now the only code that created it
lived in the test fixtures — which meant a working test suite and an unusable local
server at the same time.

Deliberately *not* an API endpoint. An unauthenticated "create me an org and a key"
route is a full compromise of any deployment that forgets to disable it, and "disabled
in production" is a configuration flag away from being wrong. A script has to be run
by someone with database access, which is the correct bar.

    uv run python scripts/bootstrap_dev.py
    uv run python scripts/bootstrap_dev.py --project my-app --scopes ingest read write

The token is printed once and never stored — only its SHA-256 goes to the database, so
there is no way to recover it later. Losing it means issuing another.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

from proofstep_api.db.models.identity import ApiKey, Environment, Organization, Project
from proofstep_api.security import keys
from proofstep_api.services.storage import get_store
from proofstep_api.settings import get_settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DEFAULT_SCOPES = ("ingest", "read", "write", "annotate")


async def bootstrap(
    session: AsyncSession, *, org_slug: str, project_slug: str, env_name: str, scopes: list[str]
) -> tuple[Project, str]:
    org = (
        await session.execute(
            select(Organization).where(
                Organization.slug == org_slug, Organization.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if org is None:
        org = Organization(name=org_slug, slug=org_slug)
        session.add(org)
        await session.flush()
        print(f"created organization {org_slug}")
    else:
        print(f"reusing organization {org_slug}")

    project = (
        await session.execute(
            select(Project).where(
                Project.org_id == org.id,
                Project.slug == project_slug,
                Project.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if project is None:
        # Left on the schema defaults on purpose: redacted capture and 30-day retention.
        # A dev project that captures more than production would make local testing a
        # poor guide to what production actually stores.
        project = Project(org_id=org.id, name=project_slug, slug=project_slug)
        session.add(project)
        await session.flush()
        print(f"created project {project_slug}")
    else:
        print(f"reusing project {project_slug}")

    environment = (
        await session.execute(
            select(Environment).where(
                Environment.project_id == project.id, Environment.name == env_name
            )
        )
    ).scalar_one_or_none()
    if environment is None:
        environment = Environment(project_id=project.id, name=env_name)
        session.add(environment)
        await session.flush()

    generated = keys.generate(env_name)
    session.add(
        ApiKey(
            project_id=project.id,
            environment_id=environment.id,
            name="bootstrap",
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=scopes,
        )
    )
    await session.commit()
    return project, generated.token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="local")
    parser.add_argument("--project", default="local")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--scopes", nargs="+", default=list(DEFAULT_SCOPES), choices=DEFAULT_SCOPES)
    parser.add_argument(
        "--write-web-env",
        action="store_true",
        help="Also write apps/web/.env.local so the dashboard can reach the API.",
    )
    args = parser.parse_args()

    settings = get_settings()
    if settings.is_production:
        # The script would work, which is exactly the problem. Creating a
        # fully-scoped key from a shell is a development affordance.
        print("refusing to run against ENV=production", file=sys.stderr)
        return 3

    _ensure_payload_bucket(settings)

    project_id, token = asyncio.run(
        _create(
            settings.sqlalchemy_url,
            org_slug=args.org,
            project_slug=args.project,
            env_name=args.env,
            scopes=list(args.scopes),
        )
    )

    print(f"\nproject id : {project_id}")
    print(f"scopes     : {' '.join(args.scopes)}")
    print(f"\nAPI key (shown once, not recoverable):\n\n  {token}\n")

    if args.write_web_env:
        target = Path(__file__).resolve().parent.parent / "apps" / "web" / ".env.local"
        # No NEXT_PUBLIC_ prefix: that prefix is what would inline the key into the
        # browser bundle. See apps/web/src/lib/api.ts.
        target.write_text(
            "# Written by scripts/bootstrap_dev.py — local only, not committed.\n"
            "PROOFSTEP_API_URL=http://127.0.0.1:8000\n"
            f"PROOFSTEP_API_KEY={token}\n"
        )
        print(f"wrote {target}")
    else:
        print("Set it for the dashboard with:\n")
        print("  PROOFSTEP_API_URL=http://127.0.0.1:8000")
        print("  PROOFSTEP_API_KEY=<the token above>\n")
        print("in apps/web/.env.local, or re-run with --write-web-env.")

    return 0


def _ensure_payload_bucket(settings: Any) -> None:
    """Create the payload bucket if object storage is configured and reachable.

    Worth doing here because the failure it prevents is genuinely confusing: with a
    missing bucket, ingest returns 500 on every span that carries a payload over the
    inline threshold, while trace-level writes succeed. The dashboard then shows a
    project with no traces and the SDK reports a flush timeout, and nothing in either
    message mentions a bucket.

    A missing MinIO is not fatal — a warning, because plenty of local work never
    exceeds the inline threshold and the server itself starts fine without it.
    """
    try:
        # `get_store` itself reaches the network — it verifies the bucket while
        # constructing the client — so the call has to be inside the guard, not before
        # it. It was outside at first, and an unreachable MinIO produced a boto
        # traceback instead of the advice below.
        store = get_store(settings)
        ensure = getattr(store, "ensure_bucket", None)
        if ensure is None:
            return
        ensure()
        print(f"payload bucket {store.bucket} ready")
    except Exception as exc:
        print(
            f"warning: could not prepare the payload bucket ({exc.__class__.__name__}). "
            "Large payloads will fail to ingest until object storage is up — "
            "try `make dev`.",
            file=sys.stderr,
        )


async def _create(
    url: str, *, org_slug: str, project_slug: str, env_name: str, scopes: list[str]
) -> tuple[uuid.UUID, str]:
    """The database half, kept separate from the printing and file writing.

    Splitting it this way keeps blocking I/O — writing `.env.local` — out of the event
    loop, and makes `bootstrap` testable against a session without a live engine.
    """
    engine = create_async_engine(url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            project, token = await bootstrap(
                session,
                org_slug=org_slug,
                project_slug=project_slug,
                env_name=env_name,
                scopes=scopes,
            )
            return project.id, token
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
