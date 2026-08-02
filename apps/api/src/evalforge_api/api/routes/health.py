"""Liveness and readiness.

Liveness must never depend on the database. If it did, a brief Postgres blip would
make the orchestrator kill every healthy API pod, turning a recoverable dependency
outage into a full restart storm.
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
    from evalforge_api.main import check_database  # noqa: PLC0415 — avoids a cycle

    database_ok = await check_database()
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if database_ok else "not_ready",
        "checks": {"database": "ok" if database_ok else "unavailable"},
    }
