"""OTLP/HTTP trace receiver.

`POST /v1/otlp/v1/traces` — the path an OpenTelemetry exporter expects when its endpoint is
set to `.../v1/otlp`, so pointing an instrumented app here is one environment variable:

    OTEL_EXPORTER_OTLP_ENDPOINT=https://your-proofstep/v1/otlp
    OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer ps_prod_...

## Protocol compliance is not optional here

An OTLP client is a *retrying* client. Getting the response wrong does not produce a
confusing error message — it produces a client that hammers the endpoint forever, or one
that silently discards data. So:

- **Success is `ExportTraceServiceResponse`**, in the same encoding as the request. A bare
  `{}` or a plain-text OK makes some SDKs log a protocol error on every export.
- **Partially rejected spans go in `partial_success`**, with a 200. The spec is explicit
  that a partial failure is not an error status; returning 4xx would make the client retry
  the whole batch, including the spans that were accepted, forever.
- **A malformed body is 400 and must not be retried.** Retrying an unparseable payload can
  never succeed, and an OTLP client honours that distinction.
- **A server fault is 503**, which *is* retryable, so a transient database problem does not
  cost the client its buffered spans.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from google.protobuf.json_format import MessageToJson
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTracePartialSuccess,
    ExportTraceServiceResponse,
)

from proofstep_api.api.dependencies import SessionDep, SettingsDep, get_principal
from proofstep_api.db.models.identity import Project
from proofstep_api.errors import ApiError, BadRequestError, ForbiddenError
from proofstep_api.otlp import receiver
from proofstep_api.otlp.decode import (
    JSON_CONTENT_TYPE,
    PROTOBUF_CONTENT_TYPE,
    OtlpDecodeError,
    decode,
)
from proofstep_api.security.permissions import Permission, Principal
from proofstep_api.services.ingest import IngestService
from proofstep_api.services.storage import get_store

logger = logging.getLogger("proofstep.otlp")

# The `/v1/otlp` prefix plus OTLP's own `/v1/traces` suffix. Nested rather than flattened
# to `/v1/otlp-traces` because the exporter appends `/v1/traces` itself and cannot be told
# not to.
router = APIRouter(prefix="/v1/otlp", tags=["otlp"])


async def require_ingest(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Principal:
    if not principal.can(Permission.TRACE_INGEST):
        raise ForbiddenError("This credential cannot ingest traces; it needs the 'ingest' scope.")
    if principal.project_id is None:
        raise ForbiddenError("Ingestion requires a project-scoped credential.")
    return principal


IngesterDep = Annotated[Principal, Depends(require_ingest)]


@router.post("/v1/traces", include_in_schema=True, summary="OTLP/HTTP trace export")
async def export_traces(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    principal: IngesterDep,
) -> Response:
    body = await request.body()
    content_type = request.headers.get("content-type", PROTOBUF_CONTENT_TYPE)
    wants_json = content_type.split(";")[0].strip().lower() == JSON_CONTENT_TYPE

    try:
        scopes = decode(body, content_type)
    except OtlpDecodeError as exc:
        # 400, not 422: OTLP clients treat 4xx other than 429 as permanent and stop
        # retrying, which is the correct behaviour for a body that can never parse.
        raise BadRequestError(str(exc)) from exc

    translation = receiver.translate(scopes)
    if not translation.batch.spans and not translation.rejected:
        # An empty export is legal and happens on exporter shutdown. Answering it with a
        # well-formed empty response is cheaper than a database round trip.
        return _respond(rejected=0, message="", wants_json=wants_json)

    project = await session.get(Project, principal.project)
    if project is None or project.deleted_at is not None:
        raise ForbiddenError("The project for this key no longer exists.")

    try:
        service = IngestService(
            session,
            project=project,
            store=get_store(settings),
            payload_ttl_days=project.retention_days_payloads,
        )
        result = await service.ingest(translation.batch)
    except ApiError:
        raise
    except Exception as exc:
        # 503 so the client retries. Losing a batch to a transient database problem would
        # cost data that the exporter still had in hand.
        logger.exception("otlp ingest failed", extra={"project_id": str(project.id)})
        msg = "Could not store the exported spans. Retry."
        raise _UnavailableError(msg) from exc

    rejected = translation.rejected_count + len(result.rejected)
    reasons = "; ".join(
        part
        for part in (
            translation.rejection_summary,
            "; ".join(f"{item.identifier}: {item.reason}" for item in result.rejected[:10]),
        )
        if part
    )
    if rejected:
        logger.info(
            "otlp partial success: %s span(s) rejected — %s",
            rejected,
            reasons,
            extra={"project_id": str(project.id)},
        )
    return _respond(rejected=rejected, message=reasons, wants_json=wants_json)


class _UnavailableError(ApiError):
    """503 — the client should retry."""

    status_code = 503
    error_type = "service_unavailable"
    title = "Service unavailable"


def _respond(*, rejected: int, message: str, wants_json: bool) -> Response:
    """An `ExportTraceServiceResponse`, in the request's encoding.

    Always 200, even with rejections: OTLP's partial-success is explicitly not an error
    status, and returning one would make the client retry the accepted spans too.
    """
    response = ExportTraceServiceResponse()
    if rejected:
        response.partial_success.CopyFrom(
            ExportTracePartialSuccess(
                rejected_spans=rejected,
                # The spec asks for a human-readable message here, and it is the only channel
                # a misconfigured exporter has for finding out what is wrong.
                error_message=message[:2000] or "spans rejected",
            )
        )

    if wants_json:
        return Response(
            content=MessageToJson(response, indent=0),
            media_type=JSON_CONTENT_TYPE,
            status_code=200,
        )
    return Response(
        content=response.SerializeToString(),
        media_type=PROTOBUF_CONTENT_TYPE,
        status_code=200,
    )


__all__ = ["export_traces", "router"]
