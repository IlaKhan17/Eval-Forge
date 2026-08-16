"""Liveness, readiness, and a security self-check.

Liveness must never depend on the database. If it did, a brief Postgres blip would
make the orchestrator kill every healthy API pod, turning a recoverable dependency
outage into a full restart storm.

Readiness reports the row-level-security state as a **warning, not a failure**. That
split is deliberate: an instance whose RLS is bypassed still serves correct traffic —
the repository predicate is layer 1 and does the actual filtering — so refusing
readiness would take a working deployment offline over a defence-in-depth gap. But it
is exactly the kind of gap that is invisible from the outside and stays broken for
months, so it has to be reported somewhere an operator will see it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness — is the process running?")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness — can this instance serve traffic?")
async def readyz(response: Response) -> dict[str, Any]:
    from proofstep_api.main import check_database  # noqa: PLC0415 — avoids a cycle

    database_ok = await check_database()
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    checks: dict[str, Any] = {"database": "ok" if database_ok else "unavailable"}
    warnings: list[str] = []
    if database_ok:
        state = await _rls_state()
        checks["row_level_security"] = state["status"]
        warnings.extend(state["warnings"])

    payload: dict[str, Any] = {
        "status": "ready" if database_ok else "not_ready",
        "checks": checks,
    }
    if warnings:
        payload["warnings"] = warnings
    return payload


async def _rls_state() -> dict[str, Any]:
    """Whether tenant isolation is actually enforced at the database layer.

    Reported because every way RLS stops working leaves the application behaving identically. The
    common one is not a missing policy — it is connecting as a superuser, which is exempt from every
    policy regardless of `FORCE`. This repository's own development role is one, so the check earns
    its place immediately.
    """
    from proofstep_api.db.rls import PROTECTED_TABLES, verify_enforced  # noqa: PLC0415
    from proofstep_api.db.session import get_engine  # noqa: PLC0415

    try:
        async with get_engine().connect() as connection:
            state = await verify_enforced(connection)
    except Exception:
        return {"status": "unknown", "warnings": ["could not determine row-level-security state"]}

    if state["problems"]:
        return {"status": "not_enforced", "warnings": state["problems"][:5]}
    return {
        "status": f"enforced ({len(state['enforced'])}/{len(PROTECTED_TABLES)} tables)",
        "warnings": [],
    }
