"""Trace read APIs, exercised over HTTP against real Postgres.

Covers the full path the dashboard will use: authenticate with an API key, list with
filters and keyset pagination, fetch a trace with its span tree.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import pytest
import pytest_asyncio
from evalforge_api.api.dependencies import get_session
from evalforge_api.api.schemas.ingest import IngestBatch
from evalforge_api.main import create_app
from evalforge_api.services.ingest import IngestService
from evalforge_api.services.storage import InMemoryObjectStore, set_store
from evalforge_api.settings import Settings
from factories import Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An app wired to the test session, so fixtures and requests share a view."""
    settings = Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32")
    app = create_app(settings)
    set_store(InMemoryObjectStore())

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    set_store(None)


def auth(tenant: Tenant) -> dict[str, str]:
    return {"Authorization": f"Bearer {tenant.token}"}


async def seed(
    session: AsyncSession, tenant: Tenant, *, count: int = 3, name: str = "workflow"
) -> None:
    service = IngestService(session, project=tenant.project, store=InMemoryObjectStore())
    for i in range(count):
        started = BASE + timedelta(minutes=i)
        await service.ingest(
            IngestBatch.model_validate(
                {
                    "traces": [
                        {
                            "trace_id": f"tr-{i}",
                            "name": name,
                            "started_at": started.isoformat(),
                            "ended_at": (started + timedelta(seconds=1)).isoformat(),
                            "status": "error" if i == 0 else "ok",
                        }
                    ],
                    "spans": [
                        {
                            "trace_id": f"tr-{i}",
                            "span_id": "root",
                            "name": "root",
                            "span_type": "agent",
                            "started_at": started.isoformat(),
                            "ended_at": (started + timedelta(seconds=1)).isoformat(),
                        },
                        {
                            "trace_id": f"tr-{i}",
                            "span_id": "child",
                            "parent_span_id": "root",
                            "name": "gmail.send",
                            "span_type": "tool",
                            "tool_name": "gmail.send",
                            "tool_args": {"to": "a@example.com"},
                            "status": "error" if i == 0 else "ok",
                            "started_at": (started + timedelta(milliseconds=10)).isoformat(),
                            "ended_at": (started + timedelta(milliseconds=90)).isoformat(),
                        },
                    ],
                }
            )
        )
    await session.flush()


class TestAuthentication:
    async def test_a_valid_key_can_list(self, client: AsyncClient, tenant_a: Tenant) -> None:
        response = await client.get("/v1/traces", headers=auth(tenant_a))
        assert response.status_code == 200

    async def test_no_credential_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/v1/traces")
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_a_garbage_key_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/v1/traces", headers={"Authorization": "Bearer nonsense"})
        assert response.status_code == 401

    async def test_an_ingest_only_key_cannot_read(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """A key leaked from a container image must not become a data export."""
        from factories import make_tenant

        limited = await make_tenant(session, slug="ingest-only", scopes=["ingest"])
        await session.flush()
        response = await client.get("/v1/traces", headers=auth(limited))
        assert response.status_code == 403
        assert "read" in response.json()["detail"]


class TestListing:
    async def test_traces_are_listed_newest_first(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await seed(session, tenant_a, count=3)
        body = (await client.get("/v1/traces", headers=auth(tenant_a))).json()
        assert [t["trace_id"] for t in body["data"]] == ["tr-2", "tr-1", "tr-0"]

    async def test_rollups_are_present_without_touching_the_span_table(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await seed(session, tenant_a, count=1)
        trace = (await client.get("/v1/traces", headers=auth(tenant_a))).json()["data"][0]
        assert trace["span_count"] == 2
        assert trace["error_count"] == 1

    async def test_filter_by_name(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await seed(session, tenant_a, count=2, name="alpha")
        assert (await client.get("/v1/traces?name=absent", headers=auth(tenant_a))).json()[
            "data"
        ] == []
        assert (
            len((await client.get("/v1/traces?name=alpha", headers=auth(tenant_a))).json()["data"])
            == 2
        )

    async def test_filter_by_error(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await seed(session, tenant_a, count=3)
        body = (await client.get("/v1/traces?has_errors=true", headers=auth(tenant_a))).json()
        assert [t["trace_id"] for t in body["data"]] == ["tr-0"]

    async def test_filter_by_time_window(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await seed(session, tenant_a, count=3)
        # quote() matters: an unencoded `+00:00` offset decodes to a space and the
        # timestamp fails to parse.
        since = quote((BASE + timedelta(minutes=1)).isoformat())
        body = (await client.get(f"/v1/traces?since={since}", headers=auth(tenant_a))).json()
        assert len(body["data"]) == 2


class TestKeysetPagination:
    async def test_pages_cover_every_row_exactly_once(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The property OFFSET pagination loses when rows are inserted mid-scan."""
        await seed(session, tenant_a, count=7)

        seen: list[str] = []
        url = "/v1/traces?limit=3"
        for _ in range(5):
            body = (await client.get(url, headers=auth(tenant_a))).json()
            seen.extend(t["trace_id"] for t in body["data"])
            if not body["has_more"]:
                break
            url = f"/v1/traces?limit=3&cursor={body['next_cursor']}"

        assert len(seen) == 7
        assert len(set(seen)) == 7

    async def test_the_last_page_reports_no_more(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await seed(session, tenant_a, count=2)
        body = (await client.get("/v1/traces?limit=50", headers=auth(tenant_a))).json()
        assert body["has_more"] is False
        assert body["next_cursor"] is None

    async def test_a_tampered_cursor_is_rejected(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        """An unsigned cursor would be a client-controlled WHERE clause."""
        response = await client.get("/v1/traces?cursor=bm90LXJlYWw.c2ln", headers=auth(tenant_a))
        assert response.status_code >= 400

    async def test_the_limit_is_capped(self, client: AsyncClient, tenant_a: Tenant) -> None:
        assert (
            await client.get("/v1/traces?limit=5000", headers=auth(tenant_a))
        ).status_code == 422


class TestTraceDetail:
    async def test_the_span_tree_comes_back_in_order(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await seed(session, tenant_a, count=1)
        body = (await client.get("/v1/traces/tr-0", headers=auth(tenant_a))).json()

        assert [s["span_id"] for s in body["spans"]] == ["root", "child"]
        assert body["spans"][1]["parent_span_id"] == "root"
        assert body["spans"][1]["tool_name"] == "gmail.send"
        assert body["spans"][1]["tool_args"] == {"to": "a@example.com"}

    async def test_an_unknown_trace_is_404(self, client: AsyncClient, tenant_a: Tenant) -> None:
        response = await client.get("/v1/traces/does-not-exist", headers=auth(tenant_a))
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_an_offloaded_payload_is_resolved_transparently(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The caller should not have to know where a payload was stored."""
        store = InMemoryObjectStore()
        set_store(store)
        big = {"document": "x" * 50_000}
        service = IngestService(session, project=tenant_a.project, store=store)
        await service.ingest(
            IngestBatch.model_validate(
                {
                    "traces": [{"trace_id": "big", "name": "w", "started_at": BASE.isoformat()}],
                    "spans": [
                        {
                            "trace_id": "big",
                            "span_id": "s1",
                            "name": "s1",
                            "span_type": "tool",
                            "started_at": BASE.isoformat(),
                            "ended_at": BASE.isoformat(),
                            "input": big,
                        }
                    ],
                }
            )
        )
        await session.flush()

        body = (await client.get("/v1/traces/big", headers=auth(tenant_a))).json()
        assert body["spans"][0]["input"] == big
        assert body["spans"][0]["input_truncated"] is False


class TestCrossTenantReads:
    async def test_one_project_cannot_list_anothers_traces(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        await seed(session, tenant_a, count=2)
        assert (await client.get("/v1/traces", headers=auth(tenant_b))).json()["data"] == []

    async def test_fetching_another_tenants_trace_by_id_is_404_not_403(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        """403 would confirm the trace exists, which is itself a disclosure."""
        await seed(session, tenant_a, count=1)
        response = await client.get("/v1/traces/tr-0", headers=auth(tenant_b))
        assert response.status_code == 404


class TestIngestEndpoint:
    async def test_a_batch_is_accepted_over_http(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        payload: dict[str, Any] = {
            "resource": {"environment": "production"},
            "traces": [{"trace_id": "http-1", "name": "w", "started_at": BASE.isoformat()}],
            "spans": [
                {
                    "trace_id": "http-1",
                    "span_id": "s1",
                    "name": "s1",
                    "span_type": "tool",
                    "started_at": BASE.isoformat(),
                    "ended_at": BASE.isoformat(),
                }
            ],
        }
        response = await client.post("/v1/ingest/traces", json=payload, headers=auth(tenant_a))

        # 202, not 201: post-processing is asynchronous, so "created" would promise
        # a consistency the pipeline does not offer.
        assert response.status_code == 202
        assert response.json()["accepted_spans"] == 1

    async def test_a_read_only_key_cannot_ingest(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        from factories import make_tenant

        reader = await make_tenant(session, slug="reader", scopes=["read"])
        await session.flush()
        response = await client.post(
            "/v1/ingest/traces", json={"traces": [], "spans": []}, headers=auth(reader)
        )
        assert response.status_code == 403

    async def test_malformed_json_is_a_clean_400(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        response = await client.post(
            "/v1/ingest/traces",
            content=b"{not json",
            headers={**auth(tenant_a), "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_a_gzip_bomb_is_refused(self, client: AsyncClient, tenant_a: Tenant) -> None:
        """Decompressed size is checked while decompressing, not after."""
        import gzip

        bomb = gzip.compress(b"a" * (200 * 1024 * 1024))
        response = await client.post(
            "/v1/ingest/traces",
            content=bomb,
            headers={**auth(tenant_a), "Content-Encoding": "gzip"},
        )
        assert response.status_code == 413

    async def test_a_body_supplied_project_id_is_ignored(
        self, client: AsyncClient, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        """Tenancy comes from the key. Trusting the body would be the classic breach."""
        payload = {
            "project_id": str(tenant_b.project.id),
            "traces": [{"trace_id": "smuggled", "name": "w", "started_at": BASE.isoformat()}],
            "spans": [],
        }
        await client.post("/v1/ingest/traces", json=payload, headers=auth(tenant_a))

        assert (await client.get("/v1/traces", headers=auth(tenant_b))).json()["data"] == []
        mine = (await client.get("/v1/traces", headers=auth(tenant_a))).json()["data"]
        assert [t["trace_id"] for t in mine] == ["smuggled"]


class TestResponseHeaders:
    async def test_every_response_carries_a_request_id(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        response = await client.get("/v1/traces", headers=auth(tenant_a))
        request_id = response.headers["X-Request-Id"]
        # It is the handle a self-hoster quotes in a bug report, so it must be
        # present and stable within the response.
        assert request_id
        assert len(request_id) >= 8

    async def test_security_headers_are_present(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        response = await client.get("/v1/traces", headers=auth(tenant_a))
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
