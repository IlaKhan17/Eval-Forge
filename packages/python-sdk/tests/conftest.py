"""SDK test fixtures.

Every test runs with `PROOFSTEP_STRICT=1`, which turns the never-raise wrapper into
a re-raise. Swallowing bugs is correct in production and catastrophic in tests: it
would let the SDK silently record nothing while every behavioural assertion passed.
The one test that verifies swallowing does so explicitly.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("PROOFSTEP_STRICT", "1")

from doubles import RecordingTransport  # noqa: F401 — re-exported for fixtures

import proofstep
from proofstep import safety
from proofstep.config import Config


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """Fresh client per test, and no leaked env between tests."""
    for key in list(os.environ):
        if key.startswith("PROOFSTEP_") and key != "PROOFSTEP_STRICT":
            del os.environ[key]
    proofstep.reset()
    safety.reset_log_throttle()
    yield
    proofstep.reset()


@pytest.fixture
def config() -> Config:
    """Records locally, never exports."""
    return Config(project="test", api_key=None, export=False, sample_rate=1.0)


@pytest.fixture
def client(config: Config) -> proofstep.Client:
    instance = proofstep.Client(config)
    proofstep._client = instance  # the module-level API should use this one
    return instance
