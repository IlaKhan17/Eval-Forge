"""The OTLP endpoint end to end: protobuf in, rows out, read back through the trace API.

The acceptance criterion for this phase is "an app instrumented with plain OpenTelemetry
plus OpenInference appears with correct span types and token counts, with no Proofstep SDK
installed". That is a round-trip claim, so these tests exercise the real route against a
real database and then read the result back through the same endpoint the dashboard uses.

The protocol tests matter as much as the data ones. An OTLP client is a *retrying* client:
the wrong status code does not produce a confusing message, it produces an exporter that
hammers the endpoint forever or silently discards data.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from factories import Tenant, make_tenant
from google.protobuf.json_format import MessageToDict
from httpx import ASGITransport, AsyncClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from proofstep_api.api.dependencies import get_session
from proofstep_api.main import create_app
from proofstep_api.otlp.decode import JSON_CONTENT_TYPE, PROTOBUF_CONTENT_TYPE, kv
from proofstep_api.settings import Settings
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

OTLP_PATH = "/v1/otlp/v1/traces"
TRACE_ID = bytes.fromhex("4bf92f3577b34da6a3ce929d0e0e4736")
ROOT_ID = bytes.fromhex("00f067aa0ba902b7")
LLM_ID = bytes.fromhex("00f067aa0ba902b8")
TOOL_ID = bytes.fromhex("00f067aa0ba902b9")
BASE_NANOS = 1_767_225_600_000_000_000  # 2026-01-01T00:00:00Z


def langgraph_export(*, trace_id: bytes = TRACE_ID) -> ExportTraceServiceRequest:
    """What an OpenInference-instrumented LangGraph agent actually sends.

    Hand-built rather than captured, so the attribute names are visible in the test — the
    thing under test is precisely whether those names are understood.
    """
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    for key, value in {
        "service.name": "sdr-agent",
        "deployment.environment.name": "production",
        "service.version": "1a2b3c4d",
    }.items():
        resource_spans.resource.attributes.append(kv(key, value))

    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = "openinference.instrumentation.langchain"
    scope_spans.scope.version = "0.1.29"

    def add(
        span_id: bytes,
        name: str,
        attributes: dict[str, Any],
        *,
        parent: bytes | None = None,
        offset_nanos: int = 0,
        duration_nanos: int = 100_000_000,
        status_code: int = 0,
    ) -> None:
        span = scope_spans.spans.add()
        span.trace_id = trace_id
        span.span_id = span_id
        if parent is not None:
            span.parent_span_id = parent
        span.name = name
        span.kind = 1
        span.start_time_unix_nano = BASE_NANOS + offset_nanos
        span.end_time_unix_nano = BASE_NANOS + offset_nanos + duration_nanos
        span.status.code = status_code
        for key, value in attributes.items():
            span.attributes.append(kv(key, value))

    add(
        ROOT_ID,
        "sdr.draft_reply",
        {
            "openinference.span.kind": "AGENT",
            "session.id": "conv-42",
            "user.id": "prospect-7",
            "input.value": json.dumps({"subject": "Re: pricing"}),
            "input.mime_type": "application/json",
        },
        duration_nanos=400_000_000,
    )
    add(
        LLM_ID,
        "ChatAnthropic",
        {
            "openinference.span.kind": "LLM",
            "llm.model_name": "claude-sonnet-5",
            "llm.provider": "anthropic",
            "llm.token_count.prompt": 412,
            "llm.token_count.completion": 96,
            "llm.token_count.total": 508,
            "llm.cost.total": "0.00381",
            "output.value": json.dumps({"intent": "meeting_requested"}),
            "output.mime_type": "application/json",
            "llm.invocation_parameters": json.dumps({"temperature": 0}),
            "acme.tenant": "northwind",
        },
        parent=ROOT_ID,
        offset_nanos=20_000_000,
    )
    add(
        TOOL_ID,
        "gmail.send",
        {
            "openinference.span.kind": "TOOL",
            "tool.name": "gmail.send",
            "input.value": json.dumps({"to": "buyer@example.com"}),
        },
        parent=ROOT_ID,
        offset_nanos=200_000_000,
    )
    return request


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The real app, with the test session injected.

    The route is exercised rather than the service, because half of what this phase promises
    is protocol behaviour: content-type negotiation, partial success, and status codes that
    an exporter interprets.
    """
    # No S3 endpoint, so payload offloading uses the in-memory store — the same choice the
    # other API tests make, and the reason this suite needs only Postgres.
    app = create_app(Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32"))

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest_asyncio.fixture
async def ingest_tenant(session: AsyncSession) -> Tenant:
    return await make_tenant(session, slug="otlp", scopes=["ingest", "read"])


def auth(tenant: Tenant) -> dict[str, str]:
    return {"authorization": f"Bearer {tenant.token}"}


class TestRoundTrip:
    async def test_a_langgraph_export_appears_with_correct_types_and_tokens(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        """The phase's acceptance criterion, end to end and with no Proofstep SDK."""
        response = await client.post(
            OTLP_PATH,
            content=langgraph_export().SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        assert response.status_code == 200

        detail = await client.get(f"/v1/traces/{TRACE_ID.hex()}", headers=auth(ingest_tenant))
        assert detail.status_code == 200
        trace = detail.json()

        # The trace was synthesized from the root span, which is the only place its name
        # exists — OTLP has no trace concept.
        assert trace["name"] == "sdr.draft_reply"
        assert trace["span_count"] == 3
        assert trace["total_tokens"] == 508
        assert float(trace["total_cost"]) == pytest.approx(0.00381)
        assert trace["git_commit"] == "1a2b3c4d"

        by_name = {span["name"]: span for span in trace["spans"]}
        assert by_name["sdr.draft_reply"]["span_type"] == "agent"
        assert by_name["ChatAnthropic"]["span_type"] == "llm"
        assert by_name["gmail.send"]["span_type"] == "tool"

        llm = by_name["ChatAnthropic"]
        assert llm["model"] == "claude-sonnet-5"
        assert llm["provider"] == "anthropic"
        assert llm["total_tokens"] == 508
        # Parsed into structure, not left as a JSON string: evaluators resolve paths like
        # `output.intent`, and a string has no paths inside it.
        assert llm["output"] == {"intent": "meeting_requested"}

        tool = by_name["gmail.send"]
        assert tool["tool_name"] == "gmail.send"
        assert tool["tool_args"] == {"to": "buyer@example.com"}

        # The span tree survived.
        assert by_name["ChatAnthropic"]["parent_span_id"] == ROOT_ID.hex()
        assert by_name["sdr.draft_reply"]["parent_span_id"] is None
        assert trace["orphan_span_ids"] == []

    async def test_unmapped_attributes_are_still_there(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        await client.post(
            OTLP_PATH,
            content=langgraph_export().SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        trace = (
            await client.get(f"/v1/traces/{TRACE_ID.hex()}", headers=auth(ingest_tenant))
        ).json()
        llm = next(s for s in trace["spans"] if s["name"] == "ChatAnthropic")
        assert llm["attributes"]["acme.tenant"] == "northwind"
        assert llm["attributes"]["otel.scope.name"] == "openinference.instrumentation.langchain"
        # And a promoted attribute is not duplicated into the bag.
        assert "llm.model_name" not in llm["attributes"]

    async def test_json_and_protobuf_produce_identical_rows(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        # The same application must not report differently because of a transport setting it
        # did not think was semantic.
        proto_trace = bytes.fromhex("1" * 32)
        json_trace = bytes.fromhex("2" * 32)

        await client.post(
            OTLP_PATH,
            content=langgraph_export(trace_id=proto_trace).SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        await client.post(
            OTLP_PATH,
            content=json.dumps(
                MessageToDict(
                    langgraph_export(trace_id=json_trace), preserving_proto_field_name=True
                )
            ),
            headers={**auth(ingest_tenant), "content-type": JSON_CONTENT_TYPE},
        )

        first = (
            await client.get(f"/v1/traces/{proto_trace.hex()}", headers=auth(ingest_tenant))
        ).json()
        second = (
            await client.get(f"/v1/traces/{json_trace.hex()}", headers=auth(ingest_tenant))
        ).json()

        def comparable(trace: dict[str, Any]) -> Any:
            return {
                "name": trace["name"],
                "span_count": trace["span_count"],
                "total_tokens": trace["total_tokens"],
                "types": sorted((s["name"], s["span_type"]) for s in trace["spans"]),
                "outputs": sorted(
                    (s["name"], json.dumps(s["output"], sort_keys=True)) for s in trace["spans"]
                ),
            }

        assert comparable(first) == comparable(second)

    async def test_a_second_batch_of_child_spans_keeps_the_trace_name(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        """The multi-batch case, which is the normal behaviour of `BatchSpanProcessor`.

        A later batch carries no root span, so it declares no trace. If the ingest service
        let that stub overwrite the declaration, the trace would revert to "unknown" — and
        because OTLP splits traces across batches routinely, that would happen to most
        traces rather than being an edge case.
        """
        full = langgraph_export()
        root_only = ExportTraceServiceRequest()
        root_only.CopyFrom(full)
        del root_only.resource_spans[0].scope_spans[0].spans[1:]

        children_only = ExportTraceServiceRequest()
        children_only.CopyFrom(full)
        del children_only.resource_spans[0].scope_spans[0].spans[0]

        await client.post(
            OTLP_PATH,
            content=root_only.SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        await client.post(
            OTLP_PATH,
            content=children_only.SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )

        trace = (
            await client.get(f"/v1/traces/{TRACE_ID.hex()}", headers=auth(ingest_tenant))
        ).json()
        assert trace["name"] == "sdr.draft_reply"
        assert trace["span_count"] == 3

    async def test_re_exporting_the_same_batch_does_not_double_count(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        # OTLP clients retry. A retry that doubled the span count would make every metric
        # derived from it wrong, and retries are the normal case rather than the exception.
        body = langgraph_export().SerializeToString()
        for _ in range(3):
            await client.post(
                OTLP_PATH,
                content=body,
                headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
            )

        trace = (
            await client.get(f"/v1/traces/{TRACE_ID.hex()}", headers=auth(ingest_tenant))
        ).json()
        assert trace["span_count"] == 3
        assert trace["total_tokens"] == 508


class TestProtocol:
    async def test_the_response_is_a_protobuf_export_response(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        # A bare `{}` or a plain-text OK makes some SDKs log a protocol error on every
        # export.
        response = await client.post(
            OTLP_PATH,
            content=langgraph_export().SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        assert response.headers["content-type"].startswith(PROTOBUF_CONTENT_TYPE)
        parsed = ExportTraceServiceResponse()
        parsed.ParseFromString(response.content)
        assert parsed.partial_success.rejected_spans == 0

    async def test_a_json_request_gets_a_json_response(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        response = await client.post(
            OTLP_PATH,
            content=json.dumps({"resourceSpans": []}),
            headers={**auth(ingest_tenant), "content-type": JSON_CONTENT_TYPE},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(JSON_CONTENT_TYPE)
        assert isinstance(response.json(), dict)

    async def test_rejected_spans_come_back_as_partial_success_with_a_200(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        """Partial failure is explicitly not an error status.

        Returning 4xx here would make the client retry the whole batch — including the spans
        that were accepted — forever.
        """
        request = langgraph_export()
        broken = request.resource_spans[0].scope_spans[0].spans.add()
        broken.trace_id = b"\x01\x02"
        broken.span_id = b"\x03"
        broken.name = "malformed"
        broken.start_time_unix_nano = BASE_NANOS

        response = await client.post(
            OTLP_PATH,
            content=request.SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        assert response.status_code == 200

        parsed = ExportTraceServiceResponse()
        parsed.ParseFromString(response.content)
        assert parsed.partial_success.rejected_spans == 1
        # The only channel a misconfigured exporter has for learning what is wrong.
        assert "trace_id" in parsed.partial_success.error_message

        # The valid spans still landed.
        trace = (
            await client.get(f"/v1/traces/{TRACE_ID.hex()}", headers=auth(ingest_tenant))
        ).json()
        assert trace["span_count"] == 3

    async def test_a_malformed_body_is_a_permanent_400(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        # Retrying an unparseable payload can never succeed, and an OTLP client honours the
        # 4xx/5xx distinction.
        response = await client.post(
            OTLP_PATH,
            content=b"not protobuf at all, not even close, definitely not a valid message",
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_an_empty_export_is_accepted(
        self, client: AsyncClient, ingest_tenant: Tenant
    ) -> None:
        # Legal, and what an exporter sends on shutdown.
        response = await client.post(
            OTLP_PATH,
            content=ExportTraceServiceRequest().SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        assert response.status_code == 200

    async def test_no_credential_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            OTLP_PATH,
            content=ExportTraceServiceRequest().SerializeToString(),
            headers={"content-type": PROTOBUF_CONTENT_TYPE},
        )
        assert response.status_code == 401

    async def test_a_read_only_credential_cannot_export(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        # The ingest scope is what separates "can look at traces" from "can write them".
        reader = await make_tenant(session, slug="otlp-reader", scopes=["read"])
        response = await client.post(
            OTLP_PATH,
            content=langgraph_export().SerializeToString(),
            headers={**auth(reader), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        assert response.status_code == 403
        assert "ingest" in response.json()["detail"]


class TestTenantIsolation:
    async def test_one_tenants_export_is_invisible_to_another(
        self, client: AsyncClient, session: AsyncSession, ingest_tenant: Tenant
    ) -> None:
        await client.post(
            OTLP_PATH,
            content=langgraph_export().SerializeToString(),
            headers={**auth(ingest_tenant), "content-type": PROTOBUF_CONTENT_TYPE},
        )
        other = await make_tenant(session, slug="otlp-other", scopes=["ingest", "read"])

        # 404, never 403: a 403 would confirm the trace exists in someone else's project.
        response = await client.get(f"/v1/traces/{TRACE_ID.hex()}", headers=auth(other))
        assert response.status_code == 404
