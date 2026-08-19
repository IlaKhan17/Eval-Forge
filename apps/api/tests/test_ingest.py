"""Ingestion behaviour against real Postgres.

The properties tested here are the ones that make the SDK's retry loop safe. If
idempotency is wrong, a network blip silently doubles every count in the product.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from factories import Tenant, make_tenant
from proofstep_api.api.schemas.ingest import IngestBatch, IngestResult
from proofstep_api.db.models.identity import Environment, Organization
from proofstep_api.db.models.traces import PayloadObject, Span, SpanEvent, Trace
from proofstep_api.services import storage
from proofstep_api.services.ingest import IngestService
from proofstep_api.services.storage import InMemoryObjectStore, load_payload
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def span(
    span_id: str,
    *,
    trace_id: str = "t1",
    parent: str | None = None,
    name: str | None = None,
    offset_ms: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    started = BASE + timedelta(milliseconds=offset_ms)
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name or span_id,
        "span_type": "tool",
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(milliseconds=50)).isoformat(),
        **extra,
    }


def batch(spans: list[dict[str, Any]], traces: list[dict[str, Any]] | None = None) -> IngestBatch:
    return IngestBatch.model_validate(
        {
            "resource": {"environment": "production"},
            "traces": traces
            if traces is not None
            else [{"trace_id": "t1", "name": "workflow", "started_at": BASE.isoformat()}],
            "spans": spans,
        }
    )


async def ingest(session: AsyncSession, tenant: Tenant, payload: IngestBatch, store=None):  # type: ignore[no-untyped-def]
    service = IngestService(session, project=tenant.project, store=store or InMemoryObjectStore())
    result = await service.ingest(payload)
    await session.flush()
    return result


async def count(session: AsyncSession, model: Any, **filters: Any) -> int:
    statement = select(func.count()).select_from(model)
    for key, value in filters.items():
        statement = statement.where(getattr(model, key) == value)
    return int((await session.execute(statement)).scalar_one())


class TestIdempotency:
    async def test_the_same_batch_twice_stores_one_copy(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The property the SDK's retry loop depends on."""
        payload = batch([span("s1"), span("s2", offset_ms=100)])

        await ingest(session, tenant_a, payload)
        await ingest(session, tenant_a, payload)

        assert await count(session, Span, project_id=tenant_a.project.id) == 2
        assert await count(session, Trace, project_id=tenant_a.project.id) == 1

    async def test_replay_does_not_inflate_the_span_count(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Counters accumulate across batches, so replay must not double them."""
        payload = batch([span("s1"), span("s2", offset_ms=100)])
        await ingest(session, tenant_a, payload)
        await ingest(session, tenant_a, payload)

        trace = (
            await session.execute(select(Trace).where(Trace.project_id == tenant_a.project.id))
        ).scalar_one()
        assert trace.span_count == 2

    async def test_a_second_batch_of_new_spans_accumulates(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """A trace's spans routinely arrive across several requests."""
        await ingest(session, tenant_a, batch([span("s1")]))
        await ingest(session, tenant_a, batch([span("s2", offset_ms=100)]))

        trace = (
            await session.execute(select(Trace).where(Trace.project_id == tenant_a.project.id))
        ).scalar_one()
        assert trace.span_count == 2

    async def test_a_resent_span_updates_rather_than_duplicating(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Sending a span open and then closed is normal, not an error."""
        await ingest(session, tenant_a, batch([span("s1", status="unset")]))
        await ingest(session, tenant_a, batch([span("s1", status="error", error_type="Boom")]))

        rows = list((await session.execute(select(Span))).scalars().all())
        assert len(rows) == 1
        assert rows[0].status == "error"

    async def test_events_are_not_duplicated_on_replay(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Events have no natural key, so replay is handled by delete-then-insert."""
        payload = batch([span("s1", events=[{"name": "retry", "timestamp": BASE.isoformat()}])])
        await ingest(session, tenant_a, payload)
        await ingest(session, tenant_a, payload)
        assert await count(session, SpanEvent, project_id=tenant_a.project.id) == 1


class TestOrderIndependence:
    async def test_a_child_arriving_before_its_parent_is_accepted(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Routine in async code, and an FK would have rejected it."""
        await ingest(session, tenant_a, batch([span("child", parent="parent", offset_ms=50)]))
        await ingest(session, tenant_a, batch([span("parent")]))
        assert await count(session, Span, project_id=tenant_a.project.id) == 2

    async def test_spans_without_a_declared_trace_create_a_stub(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Otherwise the spans would be unreachable through every read path."""
        await ingest(session, tenant_a, batch([span("s1")], traces=[]))

        trace = (
            await session.execute(select(Trace).where(Trace.project_id == tenant_a.project.id))
        ).scalar_one()
        assert trace.trace_id == "t1"
        assert trace.span_count == 1

    async def test_a_later_trace_declaration_fills_in_the_stub(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await ingest(session, tenant_a, batch([span("s1")], traces=[]))
        await ingest(
            session,
            tenant_a,
            batch(
                [], traces=[{"trace_id": "t1", "name": "real-name", "started_at": BASE.isoformat()}]
            ),
        )
        trace = (
            await session.execute(select(Trace).where(Trace.project_id == tenant_a.project.id))
        ).scalar_one()
        assert trace.name == "real-name"


class TestRollups:
    async def test_totals_are_computed_on_ingest(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """Listing traces must never aggregate over the span table."""
        await ingest(
            session,
            tenant_a,
            batch(
                [
                    span("s1", tokens={"prompt": 10, "completion": 5, "total": 15}, cost="0.001"),
                    span("s2", offset_ms=100, status="error", cost="0.002"),
                ]
            ),
        )
        trace = (
            await session.execute(select(Trace).where(Trace.project_id == tenant_a.project.id))
        ).scalar_one()
        assert trace.total_tokens == 15
        assert float(trace.total_cost) == pytest.approx(0.003)
        assert trace.error_count == 1


class TestPayloadOffload:
    async def test_small_payloads_stay_inline(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        store = InMemoryObjectStore()
        await ingest(session, tenant_a, batch([span("s1", input={"q": "hello"})]), store)

        row = (await session.execute(select(Span))).scalar_one()
        assert row.input_inline == {"q": "hello"}
        assert row.input_ref is None
        assert store.put_calls == 0

    async def test_large_payloads_are_offloaded_and_readable(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        store = InMemoryObjectStore()
        big = {"document": "x" * 50_000}
        await ingest(session, tenant_a, batch([span("s1", input=big)]), store)

        row = (await session.execute(select(Span))).scalar_one()
        assert row.input_inline is None
        assert row.input_ref is not None

        payload_row = await session.get(PayloadObject, row.input_ref)
        assert payload_row is not None
        assert load_payload(store, payload_row.object_key) == big

    async def test_identical_payloads_are_stored_once(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """A repeated system prompt is most of the bytes at real trace volumes."""
        store = InMemoryObjectStore()
        big = {"system": "y" * 50_000}
        await ingest(
            session,
            tenant_a,
            batch([span("s1", input=big), span("s2", offset_ms=10, input=big)]),
            store,
        )

        assert await count(session, PayloadObject, project_id=tenant_a.project.id) == 1
        assert len(store.objects) == 1

    async def test_an_oversized_payload_is_rejected_but_the_span_survives(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """One bad payload must not cost the span, or the trace around it."""
        result = await ingest(
            session, tenant_a, batch([span("s1", input={"huge": "z" * 2_000_000})])
        )

        assert result.accepted_spans == 1
        assert len(result.rejected) == 1
        assert "truncated" in result.rejected[0].reason

        # The span survives with a marker recording what was lost, rather than
        # appearing to have had no input at all.
        row = (await session.execute(select(Span))).scalar_one()
        inline = cast("dict[str, Any]", row.input_inline)
        assert inline["huge"]["_truncated"] is True
        assert inline["huge"]["_original_bytes"] > 1_000_000


class TestRedactionBackstop:
    async def test_a_credential_from_an_unpatched_client_is_scrubbed(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The SDK redacts first; this catches the client we do not control."""
        secret = "sk-" + "NOTAREALKEY" + "0" * 29
        result = await ingest(
            session, tenant_a, batch([span("s1", input={"note": f"token {secret}"})])
        )

        row = (await session.execute(select(Span))).scalar_one()
        assert secret not in str(row.input_inline)
        assert result.secrets_redacted >= 1

    async def test_a_secret_named_key_is_scrubbed(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        await ingest(
            session, tenant_a, batch([span("s1", tool_args={"authorization": "Bearer abc123xyz"})])
        )
        row = (await session.execute(select(Span))).scalar_one()
        assert "abc123xyz" not in str(row.args_inline)


class TestTenantIsolation:
    async def test_ingested_spans_belong_only_to_their_project(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        await ingest(session, tenant_a, batch([span("s1")]))
        await ingest(session, tenant_b, batch([span("s1")]))

        assert await count(session, Span, project_id=tenant_a.project.id) == 1
        assert await count(session, Span, project_id=tenant_b.project.id) == 1

    async def test_the_same_trace_id_in_two_projects_does_not_collide(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        """Trace ids come from the client, so two tenants will eventually collide."""
        await ingest(session, tenant_a, batch([span("s1")]))
        await ingest(session, tenant_b, batch([span("s1")]))
        assert await count(session, Trace) == 2

    async def test_payload_dedup_does_not_cross_tenants(
        self, session: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
    ) -> None:
        """Sharing an object across projects would be a cross-tenant read."""
        store = InMemoryObjectStore()
        big = {"shared": "w" * 50_000}
        await ingest(session, tenant_a, batch([span("s1", input=big)]), store)
        await ingest(session, tenant_b, batch([span("s1", input=big)]), store)

        assert await count(session, PayloadObject) == 2
        assert len(store.objects) == 2  # keyed by project, so no sharing


class TestPartitioning:
    async def test_rows_land_in_a_monthly_partition_not_the_default(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """A row in the DEFAULT partition means maintenance fell behind."""
        from sqlalchemy import text

        now = datetime.now(UTC)
        await ingest(
            session,
            tenant_a,
            batch(
                [
                    {
                        "trace_id": "now",
                        "span_id": "s1",
                        "name": "s1",
                        "span_type": "tool",
                        "started_at": now.isoformat(),
                        "ended_at": now.isoformat(),
                    }
                ],
                traces=[{"trace_id": "now", "name": "w", "started_at": now.isoformat()}],
            ),
        )
        default_rows = (
            await session.execute(text("SELECT count(*) FROM spans_default"))
        ).scalar_one()
        assert default_rows == 0

    async def test_a_span_far_in_the_past_still_ingests(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The DEFAULT partition is the safety net: old data lands, nothing 500s."""
        old = datetime(2020, 1, 1, tzinfo=UTC)
        result = await ingest(
            session,
            tenant_a,
            batch(
                [
                    {
                        "trace_id": "old",
                        "span_id": "s1",
                        "name": "s1",
                        "span_type": "tool",
                        "started_at": old.isoformat(),
                        "ended_at": old.isoformat(),
                    }
                ],
                traces=[{"trace_id": "old", "name": "w", "started_at": old.isoformat()}],
            ),
        )
        assert result.accepted_spans == 1


class TestValidation:
    async def test_an_unknown_span_type_degrades_instead_of_rejecting(self) -> None:
        """A CHECK violation mid-batch would lose the valid spans around it."""
        parsed = batch([span("s1", span_type="quantum")])
        assert parsed.spans[0].span_type == "custom"

    async def test_an_unknown_status_degrades(self) -> None:
        assert batch([span("s1", status="weird")]).spans[0].status == "unset"

    async def test_a_batch_carries_no_tenant_field(self) -> None:
        """Project comes from the key. A body-supplied project_id must be ignored."""
        parsed = IngestBatch.model_validate(
            {"project_id": str(uuid.uuid4()), "traces": [], "spans": []}
        )
        assert not hasattr(parsed, "project_id")


class BrokenObjectStore:
    """An object store that always fails, standing in for an unreachable bucket."""

    bucket = "broken"

    def put(self, key: str, body: bytes, *, content_type: str, encoding: str) -> None:
        msg = "connection refused"
        raise OSError(msg)

    def get(self, key: str) -> bytes:
        msg = "connection refused"
        raise OSError(msg)

    def presign(self, key: str, *, expires_in: int) -> str:
        msg = "connection refused"
        raise OSError(msg)

    def delete(self, key: str) -> None:
        msg = "connection refused"
        raise OSError(msg)


class TestGracefulDegradation:
    """Object storage failing must degrade ingestion, not close it.

    A trace whose payloads are missing still answers what the agent did, in what order, how long
    it took, and what it cost — which is what trajectory policies and every operational metric are
    built on. A trace that was never accepted answers nothing, and the SDK's buffer is bounded, so
    a rejected batch is data permanently gone.
    """

    async def test_a_batch_is_accepted_without_its_large_payloads(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        result = await ingest(
            session,
            tenant_a,
            batch(
                [span("s1", trace_id="degraded", output={"body": "x" * 60_000})],
                traces=[
                    {
                        "trace_id": "degraded",
                        "name": "run",
                        "started_at": BASE.isoformat(),
                    }
                ],
            ),
            store=BrokenObjectStore(),
        )
        assert result.accepted_spans == 1
        assert result.offloaded_payloads == 0

    async def test_the_dropped_payload_is_reported_not_silent(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # A payload that silently is not there is indistinguishable from a span that never had one,
        # and someone will spend an afternoon on that difference.
        result = await ingest(
            session,
            tenant_a,
            batch([span("s1", output={"body": "y" * 60_000})]),
            store=BrokenObjectStore(),
        )
        assert len(result.rejected) == 1
        assert result.rejected[0].kind == "payload"
        assert "object storage was unavailable" in result.rejected[0].reason

    async def test_the_span_skeleton_survives(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        """The part that makes degrading worthwhile.

        Timings, tokens, tool name, and parent links are all still there — so a trajectory policy
        evaluates exactly as it would have, and only the payload viewer is poorer.
        """
        await ingest(
            session,
            tenant_a,
            batch(
                [
                    span("root", span_type="agent"),
                    span(
                        "send",
                        parent="root",
                        name="gmail.send",
                        tool_name="gmail.send",
                        offset_ms=10,
                        output={"body": "z" * 60_000},
                        tokens={"prompt": 100, "completion": 20, "total": 120},
                    ),
                ]
            ),
            store=BrokenObjectStore(),
        )

        rows = (
            (
                await session.execute(
                    select(Span)
                    .where(Span.project_id == tenant_a.project.id, Span.trace_id == "t1")
                    .order_by(Span.span_id)
                )
            )
            .scalars()
            .all()
        )
        by_id = {row.span_id: row for row in rows}
        assert set(by_id) == {"root", "send"}
        assert by_id["send"].parent_span_id == "root"
        assert by_id["send"].tool_name == "gmail.send"
        assert by_id["send"].total_tokens == 120
        assert by_id["send"].duration_ms == 50
        # The payload's absence is recorded in the row rather than looking like an empty output.
        assert by_id["send"].output_inline == {"_dropped": "storage_unavailable"}

    async def test_a_small_payload_is_unaffected(
        self, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        # Payloads under the inline limit never touch object storage, so a broken bucket must not
        # affect them at all.
        await ingest(
            session,
            tenant_a,
            batch([span("s1", output={"intent": "unsubscribe"})]),
            store=BrokenObjectStore(),
        )
        row = (
            await session.execute(
                select(Span).where(Span.project_id == tenant_a.project.id, Span.span_id == "s1")
            )
        ).scalar_one()
        assert row.output_inline == {"intent": "unsubscribe"}


class TestConcurrentFirstBatch:
    """A newly deployed service's first burst must not lose batches.

    Environment auto-creation used to be check-then-insert, which is a race with exactly one
    window: the very first batches from a project that has never sent that environment name.
    Under 8-way concurrency, 11 of 200 batches came back 500 with a unique-violation on
    `uq_environments_project_id_name`. Every sequential test passed, because sequentially there
    is only ever one first batch.

    Committed on its own connections, because a race needs two real ones — the shared session
    fixture rolls back and is invisible to anyone else.
    """

    async def test_simultaneous_first_batches_all_succeed(self, engine: Any) -> None:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        slug = f"cold-start-{uuid.uuid4().hex[:8]}"
        async with maker() as setup:
            tenant = await make_tenant(setup, slug=slug)
            await setup.commit()
            org_id = tenant.org.id
            project = tenant.project

        async def send(index: int) -> IngestResult:
            async with maker() as own:
                service = IngestService(own, project=project, store=InMemoryObjectStore())
                result = await service.ingest(
                    batch(
                        [span("s1", trace_id=f"cold{index}")],
                        traces=[
                            {
                                "trace_id": f"cold{index}",
                                "name": "run",
                                "started_at": BASE.isoformat(),
                            }
                        ],
                    )
                )
                await own.commit()
                return result

        try:
            results = await asyncio.gather(*(send(i) for i in range(8)), return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            assert not failures, f"a cold-start batch was lost: {failures[0]!r}"
            assert all(r.accepted_spans == 1 for r in results)  # type: ignore[union-attr]

            # And exactly one environment row, not eight. The upsert has to converge on a single
            # row, or every later query that joins on environment sees duplicates.
            async with maker() as reader:
                count = (
                    await reader.execute(
                        select(func.count())
                        .select_from(Environment)
                        .where(Environment.project_id == project.id)
                    )
                ).scalar_one()
            assert count == 1
        finally:
            async with maker() as teardown:
                await teardown.execute(sa_delete(Organization).where(Organization.id == org_id))
                await teardown.commit()


class TestResolvingTheObjectStore:
    """Getting hold of the store, which is a separate failure from using it.

    `TestGracefulDegradation` above injects a broken store and proves `_offload` degrades. It cannot
    catch anything about how a store is *obtained*, because it never obtains one — and that is
    precisely where this broke.

    `get_store` is lazy, so the first ingest request after a restart is what constructs the client
    and verifies the bucket. When object storage was unreachable, that verification raised straight
    out of the route: past `_offload`, past every graceful-degradation test, into a 500. And since
    the store was never memoized, it was not the first request that failed but every request, for
    as long as the outage lasted. Careful degradation written, tested, and unreachable in the one
    situation it existed for.
    """

    @staticmethod
    def _settings(**overrides: object) -> SimpleNamespace:
        return SimpleNamespace(
            s3_endpoint="http://127.0.0.1:9",
            s3_bucket="proofstep-payloads",
            s3_access_key="key",
            s3_secret_key="secret",
            s3_connect_timeout_s=0.05,
            s3_read_timeout_s=0.05,
            s3_max_attempts=1,
            **overrides,
        )

    def test_an_unreachable_store_still_yields_one(self) -> None:
        storage.set_store(None)
        try:
            store = storage.get_store(self._settings())
        finally:
            storage.set_store(None)
        # A store whose bucket could not be verified is still a usable object: writes through it
        # fail, and `_offload` turns that into an accepted trace with no payload. What must not
        # happen is an exception here, which no caller is positioned to handle.
        assert store is not None

    def test_it_is_not_memoized_so_the_outage_can_end(self) -> None:
        """Otherwise recovery needs a restart.

        Memoizing a store whose bucket was never verified would be the tidier-looking fix and would
        mean payloads keep being dropped after storage comes back, until someone notices and
        restarts the API.
        """
        storage.set_store(None)
        try:
            storage.get_store(self._settings())
            assert storage._store is None, "a half-initialised store must not be cached"
        finally:
            storage.set_store(None)

    def test_a_working_store_is_memoized(self) -> None:
        # The other half of the same rule: the normal path must still build the client once per
        # process rather than per request.
        storage.set_store(None)
        try:
            first = storage.get_store(SimpleNamespace(s3_endpoint=None))
            second = storage.get_store(SimpleNamespace(s3_endpoint=None))
            assert first is second
        finally:
            storage.set_store(None)

    def test_the_client_gives_up_quickly(self) -> None:
        """Degrading is only a kindness if it is fast.

        botocore defaults to a 60-second connect timeout, a 60-second read timeout, and five
        attempts. Under those, an object store that black-holes packets does not make ingestion
        degrade — it makes every ingest request wait minutes while holding a database connection,
        and the client gives up long before the server does.
        """
        store = storage.S3ObjectStore(
            bucket="b",
            endpoint_url="http://127.0.0.1:9",
            access_key="k",
            secret_key="s",
        )
        config = store._client.meta.config
        assert config.connect_timeout <= 5, config.connect_timeout
        assert config.read_timeout <= 10, config.read_timeout
        # `total_max_attempts` is what botocore resolves to, whichever spelling went in.
        assert config.retries["total_max_attempts"] <= 3, config.retries
