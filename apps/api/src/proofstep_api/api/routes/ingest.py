"""Trace ingestion endpoint.

Returns 202, not 201: acceptance is not the same as processing. Online evaluation
and rollup refinement happen afterwards, and claiming "created" would promise a
consistency the pipeline does not offer.
"""

from __future__ import annotations

import gzip
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from proofstep_api.api.dependencies import SessionDep, SettingsDep, get_principal
from proofstep_api.api.schemas.ingest import IngestBatch, IngestResult
from proofstep_api.db.models.identity import Project
from proofstep_api.errors import BadRequestError, ForbiddenError, PayloadTooLargeError
from proofstep_api.security.permissions import Permission, Principal
from proofstep_api.services.ingest import IngestService
from proofstep_api.services.storage import get_store

router = APIRouter(prefix="/v1/ingest", tags=["ingestion"])

# A gzip bomb decompresses to orders of magnitude more than it costs to send. The
# cap is on the *decompressed* size, checked while decompressing, because checking
# afterwards means the allocation already happened.
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_DECOMPRESSION_RATIO = 100


async def _read_body(request: Request, max_bytes: int) -> bytes:
    body = await request.body()
    if len(body) > max_bytes:
        raise PayloadTooLargeError(f"Request body exceeds the {max_bytes} byte limit.")

    if request.headers.get("content-encoding", "").lower() != "gzip":
        return body

    try:
        decompressed = gzip.decompress(body)
    except (OSError, EOFError) as exc:
        raise BadRequestError("Body is not valid gzip.") from exc

    if len(decompressed) > MAX_DECOMPRESSED_BYTES:
        raise PayloadTooLargeError(
            f"Decompressed body is {len(decompressed)} bytes, over the "
            f"{MAX_DECOMPRESSED_BYTES} byte limit."
        )
    if body and len(decompressed) / len(body) > MAX_DECOMPRESSION_RATIO:
        raise PayloadTooLargeError(
            f"Compression ratio {len(decompressed) // len(body)}:1 exceeds the "
            f"{MAX_DECOMPRESSION_RATIO}:1 limit."
        )
    return decompressed


@router.post(
    "/traces",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestResult,
    summary="Ingest a batch of traces and spans",
)
async def ingest_traces(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    principal: Annotated[Principal, Depends(get_principal)],
) -> IngestResult:
    if not principal.can(Permission.TRACE_INGEST):
        raise ForbiddenError("This key does not have the 'ingest' scope.")
    if principal.project_id is None:
        raise ForbiddenError("Ingestion requires a project-scoped API key.")

    raw = await _read_body(request, settings.max_request_bytes)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BadRequestError(f"Body is not valid JSON: {exc.msg}") from exc

    batch = IngestBatch.model_validate(payload)

    project = await session.get(Project, principal.project_id)
    if project is None:
        raise ForbiddenError("The project for this key no longer exists.")

    service = IngestService(
        session,
        project=project,
        store=get_store(settings),
        payload_ttl_days=project.retention_days_payloads,
    )
    return await service.ingest(batch)
