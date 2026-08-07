"""A real stack for the acceptance test: a live API process, a real database, a real bucket.

Not an ASGI transport. The integration suites already prove the handlers work when called in
process; what only a subprocess can prove is that the CLI and the SDK reach a server over HTTP,
authenticate with a key they were handed, and act on what comes back. Every failure that lives in
that gap — a serialisation difference, a missing route, an env var the settings object never reads —
is invisible to an in-process test.

The API is started as `uvicorn` in a subprocess on a probed port, against the same Postgres the
integration tests use but a **separate database**, dropped and recreated per session. A test that
shares a database with the suite that is also mutating it fails intermittently and gets deleted.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
E2E_DB = "evalforge_e2e"

#: How long to wait for the API to answer /readyz. Generous, because a cold start also applies
#: migrations, and a flaky timeout in the acceptance test is worse than a slow one.
BOOT_TIMEOUT_S = 90


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _env(**overrides: str) -> dict[str, str]:
    """The subprocess environment: the developer's, with the database pointed at ours.

    Inherited rather than constructed, because the API needs the same Postgres credentials and S3
    settings the rest of the suite uses, and duplicating them here would mean two places to update.
    """
    env = dict(os.environ)
    env["POSTGRES_DB"] = E2E_DB
    env["ENV"] = "development"
    env.setdefault("JWT_SECRET", "e2e-only-secret-value-that-is-long-enough")
    env.update(overrides)
    return env


def _psql(sql: str) -> None:
    """Run one statement against the maintenance database.

    Via psycopg directly rather than `psql`, so the fixture does not depend on client tools being
    installed — the CI image has the Python driver by construction and may not have the binary.
    """
    import psycopg

    dsn = (
        f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ.get('POSTGRES_HOST', '127.0.0.1')}"
        f":{os.environ.get('POSTGRES_PORT', '5432')}/postgres"
    )
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def stack() -> Iterator[dict[str, Any]]:
    """A running API with a fresh database, plus a project API key.

    Session-scoped: booting uvicorn and applying migrations costs a few seconds, and the acceptance
    scenario is deliberately one long test rather than many short ones — the loop is the subject, so
    splitting it into independent tests would test the steps and not the loop.
    """
    if not os.environ.get("POSTGRES_PASSWORD"):
        pytest.skip("POSTGRES_PASSWORD is not set; source .env to run the e2e suite")

    try:
        _psql(f'DROP DATABASE IF EXISTS "{E2E_DB}" WITH (FORCE)')
        _psql(f'CREATE DATABASE "{E2E_DB}"')
    except Exception as exc:
        pytest.skip(f"postgres unavailable for the e2e suite: {exc}")

    env = _env()
    migrate = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert migrate.returncode == 0, f"migrations failed:\n{migrate.stdout}\n{migrate.stderr}"

    bootstrap = subprocess.run(
        ["uv", "run", "python", "scripts/bootstrap_dev.py", "--org", "e2e", "--project", "e2e"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bootstrap.returncode == 0, f"bootstrap failed:\n{bootstrap.stdout}{bootstrap.stderr}"
    key = next(
        (word for word in bootstrap.stdout.split() if word.startswith("ef_dev_")),
        None,
    )
    assert key, f"no API key in bootstrap output:\n{bootstrap.stdout}"

    port = _free_port()
    log_path = ROOT / ".e2e-api.log"
    log = log_path.open("w")
    api = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "evalforge_api.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if api.poll() is not None:
            log.close()
            pytest.fail(f"the API exited during startup:\n{log_path.read_text()[-4000:]}")
        try:
            if httpx.get(f"{endpoint}/readyz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.4)
    else:
        api.terminate()
        log.close()
        pytest.fail("the API never became ready")

    try:
        yield {"endpoint": endpoint, "api_key": key, "env": env, "port": port}
    finally:
        api.terminate()
        try:
            api.wait(timeout=10)
        except subprocess.TimeoutExpired:
            api.kill()
        log.close()


@pytest.fixture(scope="session")
def api(stack: dict[str, Any]) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=stack["endpoint"],
        headers={"authorization": f"Bearer {stack['api_key']}"},
        timeout=30.0,
    ) as client:
        yield client
