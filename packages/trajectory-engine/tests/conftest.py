"""Fixtures for the trajectory-engine suite."""

from __future__ import annotations

import pytest
from builders import TraceBuilder


@pytest.fixture
def trace() -> TraceBuilder:
    return TraceBuilder()
