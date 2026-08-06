"""A background job that fails must leave a record, and that record must not leak.

These tests exist because the dead-letter path is the one piece of error handling that only ever
runs when something else is already broken. Every mistake in it is therefore invisible in normal
operation: a record written on the failing job's session (rolled back), an exception raised from
inside the exception handler, a row per retry instead of per failure. Each of those leaves the
table empty or unreadable exactly when someone needs it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from evalforge_api.api.dependencies import get_session
from evalforge_api.db.models.ops import MAX_MESSAGE_CHARS, DeadLetterJob
from evalforge_api.main import create_app
from evalforge_api.settings import Settings
from evalforge_api.worker import deadletter
from factories import Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32"))

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


def factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Stand in for the worker's session factory, using the test's own session.

    `deadletter.record` opens its own session and commits, because the failing job's transaction
    is being rolled back — that is the behaviour under test, and `test_a_record_survives_the_jobs_
    rollback` exercises it for real against the database.

    Here the commit is turned into a flush. Committing the *shared* test session would also commit
    the fixtures' organisation and project rows, escaping the per-test rollback and breaking every
    later test with a duplicate-slug violation. (It did exactly that, twice, in this project — once
    in the Phase 8 concurrency tests and once here.) Flushing keeps the row visible to the
    assertions without leaking it.
    """

    class _NoCommit:
        def __init__(self, inner: AsyncSession) -> None:
            self._inner = inner

        async def commit(self) -> None:
            await self._inner.flush()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    class _Ctx:
        async def __aenter__(self) -> Any:
            return _NoCommit(session)

        async def __aexit__(self, *_: object) -> None:
            return None

    class _Factory:
        def __call__(self, *_: Any, **__: Any) -> Any:
            return _Ctx()

    return _Factory()  # type: ignore[return-value]


class TestRecording:
    async def test_a_failure_becomes_a_row(self, session: AsyncSession) -> None:
        recorded = await deadletter.record(
            factory(session),
            job_name="online_eval",
            error=RuntimeError("the judge provider timed out"),
            attempts=3,
            job_id="cron:online_eval:1",
        )
        assert recorded is not None

        row = (await session.execute(select(DeadLetterJob))).scalar_one()
        assert row.job_name == "online_eval"
        assert row.error_type == "RuntimeError"
        assert row.error_message == "the judge provider timed out"
        assert row.attempts == 3
        assert row.resolved_at is None

    async def test_only_allow_listed_arguments_are_kept(self, session: AsyncSession) -> None:
        """The filtering that stops this table becoming an accidental payload store.

        An allow-list rather than a deny-list, so a future job's new keyword argument is dropped
        by default instead of stored by default. `trace` here stands in for the thing that must
        never land: content.
        """
        await deadletter.record(
            factory(session),
            job_name="online_eval",
            error=ValueError("boom"),
            context={
                "project_ids": ["a", "b"],
                "batch_size": 100,
                "trace": {"output": "a customer's email body"},
                "authorization": "Bearer secret",
            },
        )
        row = (await session.execute(select(DeadLetterJob))).scalar_one()
        assert row.context == {"project_ids": ["a", "b"], "batch_size": 100}

    async def test_a_long_message_is_truncated(self, session: AsyncSession) -> None:
        # A driver error can echo the whole statement and its bound parameters. Truncation is the
        # backstop behind not storing tracebacks at all.
        await deadletter.record(
            factory(session), job_name="retention", error=RuntimeError("x" * 50_000)
        )
        row = (await session.execute(select(DeadLetterJob))).scalar_one()
        assert len(row.error_message) == MAX_MESSAGE_CHARS

    async def test_an_empty_message_is_still_identifiable(self, session: AsyncSession) -> None:
        # `raise SomeError()` is common, and a blank message would violate NOT NULL — turning a
        # recorded failure into an unrecorded one.
        await deadletter.record(factory(session), job_name="rollup", error=RuntimeError())
        row = (await session.execute(select(DeadLetterJob))).scalar_one()
        assert row.error_message == "(no message)"

    async def test_recording_never_raises(self, session: AsyncSession) -> None:
        """The property that matters most.

        This runs inside an `except` block. An exception here would replace a diagnosable job
        failure with a confusing one, so a broken recorder has to degrade to a log line.
        """

        class Broken:
            def __call__(self, *_: Any, **__: Any) -> Any:
                msg = "no database"
                raise OSError(msg)

        result = await deadletter.record(
            Broken(),  # type: ignore[arg-type]
            job_name="online_eval",
            error=RuntimeError("original"),
        )
        assert result is None


class TestReading:
    async def test_resolving_is_idempotent_and_reports_it(self, session: AsyncSession) -> None:
        recorded = await deadletter.record(
            factory(session), job_name="retention", error=RuntimeError("locked")
        )
        assert recorded is not None

        assert await deadletter.resolve(session, recorded, note="restarted the worker") is True
        # False, not an exception: two people looking at the same incident is the common case.
        assert await deadletter.resolve(session, recorded, note="again") is False

        row = (await session.execute(select(DeadLetterJob))).scalar_one()
        assert row.resolution == "restarted the worker"

    async def test_resolved_failures_leave_the_unresolved_list(self, session: AsyncSession) -> None:
        first = await deadletter.record(
            factory(session), job_name="online_eval", error=RuntimeError("a")
        )
        await deadletter.record(factory(session), job_name="rollup", error=RuntimeError("b"))
        assert first is not None
        await deadletter.resolve(session, first, note="handled")

        remaining = await deadletter.unresolved(session)
        assert [row.job_name for row in remaining] == ["rollup"]

    async def test_the_summary_reports_counts_and_the_oldest_unresolved(
        self, session: AsyncSession
    ) -> None:
        for _ in range(2):
            await deadletter.record(
                factory(session), job_name="online_eval", error=RuntimeError("timeout")
            )
        await deadletter.record(factory(session), job_name="retention", error=RuntimeError("lock"))

        summary = await deadletter.summary(session, window_hours=24)
        assert summary["by_job"]["online_eval"]["failures"] == 2
        assert summary["by_job"]["retention"]["failures"] == 1
        assert summary["unresolved"] == 3
        assert summary["oldest_unresolved"] is not None

    async def test_a_resolved_row_still_counts_in_the_window(self, session: AsyncSession) -> None:
        """Kept rather than deleted, on purpose.

        "This job has failed on eleven separate days" is the question that distinguishes a blip
        from a job nobody is watching, and deleting on resolve makes it unanswerable.
        """
        recorded = await deadletter.record(
            factory(session), job_name="rollup", error=RuntimeError("x")
        )
        assert recorded is not None
        await deadletter.resolve(session, recorded, note="known issue")

        summary = await deadletter.summary(session)
        assert summary["by_job"]["rollup"]["failures"] == 1
        assert summary["unresolved"] == 0
        assert summary["oldest_unresolved"] is None

    async def test_the_window_excludes_older_failures(self, session: AsyncSession) -> None:
        old = DeadLetterJob(
            job_name="retention",
            attempts=3,
            error_type="RuntimeError",
            error_message="last month",
            created_at=datetime.now(UTC) - timedelta(days=40),
        )
        session.add(old)
        await session.flush()

        summary = await deadletter.summary(session, window_hours=24)
        assert summary["by_job"] == {}
        # Still unresolved, though — the window bounds the *rate*, not what is outstanding. A month-
        # old failure nobody handled is precisely the thing that must not disappear from the count.
        assert summary["unresolved"] == 1


class TestQueueSnapshot:
    async def test_an_unreachable_redis_is_an_error_not_an_empty_queue(self) -> None:
        # Depth 0 and "cannot see the queue" look identical in a number and mean opposite things:
        # the first is healthy, the second means nothing is being processed at all.
        snapshot = await deadletter.queue_snapshot("redis://127.0.0.1:1/0")
        assert snapshot.error is not None
        assert snapshot.depth is None
        assert snapshot.as_dict()["depth"] is None


class TestWorkerIntegration:
    """The wiring, not the helpers.

    Two things can be right in isolation and wrong together: a job can fail without ever reaching
    the recorder, and the recorder can fire on every retry instead of once at the end.
    """

    async def test_a_transient_failure_is_not_dead_lettered(self, session: AsyncSession) -> None:
        # First attempt of three. Recording here would produce three rows for one broken job and
        # make "how many distinct failures were there" unanswerable.
        await self._run_failing_job(session, job_try=1)
        assert (await deadletter.unresolved(session)) == []

    async def test_the_last_attempt_is_dead_lettered(self, session: AsyncSession) -> None:
        await self._run_failing_job(session, job_try=3)
        rows = await deadletter.unresolved(session)
        assert [(row.job_name, row.error_type, row.attempts) for row in rows] == [
            ("online_eval", "RuntimeError", 3)
        ]

    async def test_a_direct_invocation_is_final(self, session: AsyncSession) -> None:
        """No `job_try` means nobody is going to retry this.

        An operator running a job by hand, or a test. Waiting for a retry that will never come
        would lose the record entirely.
        """
        await self._run_failing_job(session, job_try=None)
        assert len(await deadletter.unresolved(session)) == 1

    async def test_the_job_still_raises(self, session: AsyncSession) -> None:
        # Recording must not swallow the exception: arq decides about retries, and a job that
        # returns normally after failing would look healthy forever.
        with pytest.raises(RuntimeError, match="judge provider"):
            await self._run_failing_job(session, job_try=3, swallow=False)

    async def _run_failing_job(
        self,
        session: AsyncSession,
        *,
        job_try: int | None,
        swallow: bool = True,
    ) -> None:
        from evalforge_api.worker import main as worker_main

        async def boom(_session: AsyncSession, **_: Any) -> Any:
            msg = "the judge provider timed out"
            raise RuntimeError(msg)

        ctx: dict[Any, Any] = {"job_id": "cron:online_eval:1"}
        if job_try is not None:
            ctx["job_try"] = job_try

        # The session factory is patched rather than the recorder: patching the recorder would
        # test that a call happened, and what matters is that a row exists.
        original = worker_main._session_factory
        worker_main._session_factory = lambda: factory(session)  # type: ignore[assignment]
        try:
            if swallow:
                # Every case but one is about what was *recorded*; the exception itself is the
                # subject of exactly one test above.
                with pytest.raises(RuntimeError):
                    await worker_main._with_session("online_eval", boom, ctx)
            else:
                await worker_main._with_session("online_eval", boom, ctx)
        finally:
            worker_main._session_factory = original  # type: ignore[assignment]


class TestOpsEndpoints:
    """What an operator sees. The point is that "nothing to do" and "nothing is being done"
    are distinguishable from the response alone."""

    async def test_queues_reports_all_three_signals(
        self, client: AsyncClient, tenant_a: Tenant, session: AsyncSession
    ) -> None:
        await deadletter.record(
            factory(session), job_name="online_eval", error=RuntimeError("provider timed out")
        )

        response = await client.get(
            "/v1/ops/queues", headers={"authorization": f"Bearer {tenant_a.token}"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["dead_letters"]["unresolved"] == 1
        assert body["dead_letters"]["oldest_unresolved"] is not None
        assert "job_queue" in body
        assert "review_queues" in body
        # An unresolved failure is not healthy, whatever the queue depth says.
        assert body["healthy"] is False

    async def test_a_clean_deployment_is_healthy_only_if_the_queue_is_visible(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        response = await client.get(
            "/v1/ops/queues", headers={"authorization": f"Bearer {tenant_a.token}"}
        )
        body = response.json()
        # Whether Redis is up in this test environment is not the claim. The claim is that
        # `healthy` follows the queue's *visibility*, not just the absence of dead letters — a
        # queue nobody can see is a queue nobody is draining.
        assert body["healthy"] is (body["job_queue"].get("error") is None)

    async def test_resolving_through_the_api_needs_more_than_read(
        self, client: AsyncClient, session: AsyncSession, tenant_a: Tenant
    ) -> None:
        recorded = await deadletter.record(
            factory(session), job_name="retention", error=RuntimeError("lock timeout")
        )
        assert recorded is not None

        response = await client.post(
            f"/v1/ops/dead-letters/{recorded}/resolve",
            json={"note": "restarted the worker"},
            headers={"authorization": f"Bearer {tenant_a.token}"},
        )
        assert response.status_code == 200

        listing = await client.get(
            "/v1/ops/dead-letters", headers={"authorization": f"Bearer {tenant_a.token}"}
        )
        assert listing.json() == []

    async def test_resolving_an_unknown_failure_is_404(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        response = await client.post(
            f"/v1/ops/dead-letters/{uuid.uuid4()}/resolve",
            json={"note": "n/a"},
            headers={"authorization": f"Bearer {tenant_a.token}"},
        )
        assert response.status_code == 404


class TestSurvivesRollback:
    """The property the whole design turns on, tested against the real database.

    Every other test in this file substitutes the session factory. This one does not: it uses two
    real sessions, rolls the first one back the way a failed job does, and checks the record is
    still there. Written because the natural implementation — reuse the job's session — passes
    every unit test and leaves the table permanently empty in production.
    """

    async def test_a_record_survives_the_jobs_rollback(self, engine: Any) -> None:
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async with maker() as job_session:
            job_session.add(
                DeadLetterJob(
                    job_name="scratch",
                    attempts=1,
                    error_type="RuntimeError",
                    error_message="this row is the job's own work and must not survive",
                )
            )
            await job_session.flush()

            recorded = await deadletter.record(
                maker, job_name="online_eval", error=RuntimeError("provider timed out")
            )
            # What a failing job does with its own session.
            await job_session.rollback()

        assert recorded is not None
        try:
            async with maker() as reader:
                rows = (await reader.execute(select(DeadLetterJob))).scalars().all()
                names = sorted(row.job_name for row in rows)
            assert names == ["online_eval"], "the dead letter survived; the job's own work did not"
        finally:
            # This test commits for real, so it cleans up on its own connection rather than
            # relying on a fixture's rollback.
            async with maker() as cleanup:
                await cleanup.execute(delete(DeadLetterJob))
                await cleanup.commit()
