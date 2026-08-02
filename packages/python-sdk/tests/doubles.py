"""Test doubles for the SDK suite.

A uniquely named module rather than conftest, so this package's suite and the API's
can run in the same pytest session without their conftests shadowing each other.
"""

from __future__ import annotations

import gzip
import json
from typing import Any


class RecordingTransport:
    """Captures exported bodies instead of sending them."""

    def __init__(self, *, fail_times: int = 0, always_fail: bool = False) -> None:
        self.bodies: list[bytes] = []
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.attempts = 0

    def __call__(self, body: bytes) -> None:
        self.attempts += 1
        if self.always_fail or self.attempts <= self.fail_times:
            msg = "simulated transport failure"
            raise ConnectionError(msg)
        self.bodies.append(body)

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(gzip.decompress(b)) for b in self.bodies]

    def decoded(self) -> str:
        return "".join(gzip.decompress(b).decode("utf-8") for b in self.bodies)
