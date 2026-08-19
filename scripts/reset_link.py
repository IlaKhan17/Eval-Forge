#!/usr/bin/env python
"""Mint a password reset link for one account, as the operator.

    uv run python scripts/reset_link.py someone@example.com

Prints a link. That is the whole tool.

**Why this exists alongside the `/auth/forgot` endpoint.** There is no mail transport in this
system, so the endpoint delivers by writing the link to the application log — which works, and
requires whoever is helping to have log access and to find the right line at the right moment. This
is the direct path: an operator who can already reach the database can hand someone a link without
either of them going through a form first.

**Why it does not print the link for an unknown address.** Not for enumeration — anyone running
this already has the database — but because a typo'd address silently producing a working link for
nobody wastes the one thing this is meant to save, which is the time of a person who cannot get in.

The link is subject to every rule the endpoint's links are: hashed at rest, single use, and expiring
after `password_reset_ttl_s`. Minting one invalidates any other outstanding reset for that account,
so running this twice hands out one working link, not two.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from proofstep_api.db.models.identity import PasswordReset, User
from proofstep_api.security import resets
from proofstep_api.settings import get_settings
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def mint(email: str) -> str | None:
    settings = get_settings()
    engine = create_async_engine(settings.sqlalchemy_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            user = (
                await session.execute(select(User).where(User.email == email.lower().strip()))
            ).scalar_one_or_none()
            if user is None:
                return None

            now = datetime.now(UTC)
            # Same invalidation the endpoint performs. Two live links for one account is the
            # standing-credential problem this flow exists to avoid, and an operator issuing a
            # second one has clearly decided the first is not being used.
            await session.execute(
                update(PasswordReset)
                .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))
                .values(used_at=now)
            )
            token, digest = resets.generate()
            session.add(
                PasswordReset(
                    user_id=user.id,
                    token_hash=digest,
                    expires_at=now + timedelta(seconds=settings.password_reset_ttl_s),
                )
            )
            await session.commit()
            return resets.reset_url(token, settings=settings)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="the account to issue a reset link for")
    args = parser.parse_args()

    link = asyncio.run(mint(args.email))
    if link is None:
        print(f"No account for {args.email!r}. Check the address.", file=sys.stderr)
        return 1

    settings = get_settings()
    minutes = settings.password_reset_ttl_s // 60
    print(link)
    print(
        f"\nValid once, for {minutes} minutes. Send it over a channel you trust — it is enough to "
        "take over the account on its own.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
