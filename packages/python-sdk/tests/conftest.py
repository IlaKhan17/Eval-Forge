"""SDK test fixtures.

Every test runs with `EVALFORGE_STRICT=1`, which turns the never-raise wrapper into
a re-raise. Swallowing bugs is correct in production and catastrophic in tests: it
would let the SDK silently record nothing while every behavioural assertion passed.
The one test that verifies swallowing does so explicitly.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("EVALFORGE_STRICT", "1")

from doubles import RecordingTransport  # noqa: F401 — re-exported for fixtures

import evalforge
from evalforge import safety
from evalforge.config import Config


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Fresh client per test, and no leaked env between tests."""
    for key in list(os.environ):
        if key.startswith("EVALFORGE_") and key != "EVALFORGE_STRICT":
            del os.environ[key]
    evalforge.reset()
    safety.reset_log_throttle()
    yield
    evalforge.reset()


@pytest.fixture
def config() -> Config:
    """Records locally, never exports."""
    return Config(project="test", api_key=None, export=False, sample_rate=1.0)


@pytest.fixture
def client(config: Config) -> evalforge.Client:
    instance = evalforge.Client(config)
    evalforge._client = instance  # the module-level API should use this one
    return instance
