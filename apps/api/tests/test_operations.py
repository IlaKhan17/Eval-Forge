"""Deployment posture: the checks that only matter when something is misconfigured.

Everything here guards a property whose failure is invisible in normal operation — a role that
bypasses row-level security, a secret that never loaded, a worker that stopped, a metric exported as
0 when it should have been absent. None of these change what the API returns to a correct request,
which is exactly why they need tests of their own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from evalforge_api.api.dependencies import get_session
from evalforge_api.api.routes import metrics as metrics_module
from evalforge_api.db.models.ops import WorkerHeartbeat
from evalforge_api.main import create_app
from evalforge_api.settings import Settings
from evalforge_api.worker import deadletter
from factories import Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

SECRET = "a-signing-key-long-enough-for-production-use"


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32"))

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


class TestSecretFiles:
    """`<NAME>_FILE` exists so a secret never has to live in an environment variable.

    An env var is readable from /proc, appears in `docker inspect`, and lands in crash reports; a
    file has an owner and a mode. This is the mechanism every orchestrator already speaks.
    """

    def test_a_secret_is_read_from_its_file(self, tmp_path: Path) -> None:
        target = tmp_path / "jwt"
        target.write_text(f"{SECRET}\n")
        assert Settings(jwt_secret_file=str(target)).jwt_secret == SECRET

    def test_an_inline_value_still_works(self) -> None:
        assert Settings(jwt_secret=SECRET).jwt_secret == SECRET

    def test_an_empty_file_does_not_clobber_the_value(self, tmp_path: Path) -> None:
        # A secret volume that exists but has not been populated yet is common during a rollout.
        # Treating its blank content as the secret would replace a working key with an empty string
        # — which fails later, elsewhere, with a message about signing rather than about mounting.
        target = tmp_path / "jwt"
        target.write_text("   \n")
        assert Settings(jwt_secret=SECRET, jwt_secret_file=str(target)).jwt_secret == SECRET

    def test_an_unreadable_file_fails_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="could not read"):
            Settings(jwt_secret_file=str(tmp_path / "missing"))


class TestProductionRefusals:
    """Production settings that must stop a boot rather than degrade it."""

    def test_a_shared_migration_role_is_refused(self) -> None:
        # Running migrations as the application role means the application owns its tables, and an
        # owner is exempt from its own policies unless FORCE is set on every one — a property a
        # later migration can drop without anyone noticing.
        with pytest.raises(ValueError, match="migration_database_url"):
            Settings(
                env="production",
                jwt_secret=SECRET,
                postgres_password="x" * 20,
                migration_database_url=None,
            )

    def test_a_separate_migration_role_is_accepted(self) -> None:
        settings = Settings(
            env="production",
            jwt_secret=SECRET,
            postgres_password="x" * 20,
            migration_database_url="postgresql+psycopg://owner:pw@db/evalforge",
        )
        assert settings.migration_url != settings.sqlalchemy_url

    def test_the_migration_url_falls_back_outside_production(self) -> None:
        # A single-role development install must keep working with no extra configuration.
        settings = Settings(env="development")
        assert settings.migration_url == settings.sqlalchemy_url


class TestHeartbeats:
    async def test_a_beat_is_recorded_and_updated_in_place(self, session: AsyncSession) -> None:
        """One row per worker, not one per minute.

        History here would grow by 1440 rows a day to answer a question about the newest one.
        """
        factory = _factory(session)
        await deadletter.beat(factory, worker_name="w1", detail={"source": "test"})
        first = (await session.execute(select(WorkerHeartbeat))).scalar_one()

        await deadletter.beat(factory, worker_name="w1", detail={"source": "again"})
        # The upsert runs as Core SQL, so the ORM's identity map still holds the row as it was read.
        # Without expiring, this asserts against a stale copy and would pass even if the update had
        # done nothing.
        session.expire_all()
        rows = (await session.execute(select(WorkerHeartbeat))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == first.id
        assert rows[0].detail == {"source": "again"}

    async def test_beating_never_raises(self) -> None:
        # It runs after every job. A heartbeat that can take down a worker turns a monitoring
        # feature into an outage.
        class Broken:
            def __call__(self, *_: Any, **__: Any) -> Any:
                msg = "no database"
                raise OSError(msg)

        await deadletter.beat(Broken())  # type: ignore[arg-type]


class TestMetrics:
    async def test_it_requires_a_credential(self, client: AsyncClient) -> None:
        # Prometheus can send a bearer token from a file. Adding an unauthenticated surface to a
        # service whose threat model is "who may read what" is not worth one line of scrape config.
        assert (await client.get("/metrics")).status_code == 401

    async def test_it_exposes_the_gauges_an_alert_reads(
        self, client: AsyncClient, tenant_a: Tenant
    ) -> None:
        response = await client.get(
            "/metrics", headers={"authorization": f"Bearer {tenant_a.token}"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

        body = response.text
        for name in (
            "evalforge_up",
            "evalforge_build_info",
            "evalforge_dead_letters_unresolved",
            "evalforge_job_queue_reachable",
            "evalforge_rls_enforced",
        ):
            assert f"{name}" in body, f"{name} is missing; an alert reads it"

    async def test_an_unknown_value_is_absent_rather_than_zero(self) -> None:
        """The discipline the whole exporter is built on.

        A queue depth of 0 means empty; a queue that could not be read means "I have no idea", and a
        dashboard rendering the second as the first is how an outage gets watched over for an hour.
        """
        assert metrics_module._line("x", None) == []
        assert metrics_module._line("x", 0) == ["x 0"]

    async def test_a_worker_that_never_beat_produces_no_age_sample(
        self, client: AsyncClient, tenant_a: Tenant, session: AsyncSession
    ) -> None:
        # `absent()` is the alert for this case. An age of 0 would read as "just seen", which is the
        # opposite of the truth.
        await session.execute(delete(WorkerHeartbeat))
        await session.flush()

        body = (
            await client.get("/metrics", headers={"authorization": f"Bearer {tenant_a.token}"})
        ).text
        samples = [
            line
            for line in body.splitlines()
            if line.startswith("evalforge_worker_heartbeat_age_seconds{")
        ]
        assert samples == []
        assert "evalforge_workers_known 0" in body

    async def test_a_recorded_beat_appears_with_its_age(
        self, client: AsyncClient, tenant_a: Tenant, session: AsyncSession
    ) -> None:
        session.add(
            WorkerHeartbeat(
                worker_name="w1",
                last_seen_at=datetime.now(UTC) - timedelta(seconds=90),
                detail={},
            )
        )
        await session.flush()

        body = (
            await client.get("/metrics", headers={"authorization": f"Bearer {tenant_a.token}"})
        ).text
        sample = next(
            line
            for line in body.splitlines()
            if line.startswith('evalforge_worker_heartbeat_age_seconds{worker="w1"}')
        )
        assert 85 < float(sample.rsplit(" ", 1)[1]) < 200


def _factory(session: AsyncSession) -> Any:
    """The worker's session factory, standing in on the test's own session.

    Commit becomes flush for the same reason as in test_dead_letters.py: committing the shared
    session would escape the per-test rollback and break every later test.
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

    return _Factory()
