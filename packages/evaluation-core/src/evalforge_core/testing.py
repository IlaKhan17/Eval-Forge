"""Test doubles, shipped as part of the package.

No test in this repository may call a real model provider. A flaky test suite in a
tool that gates other people's CI destroys the product's credibility faster than any
missing feature (docs/TESTING_STRATEGY.md).

These are public so users can test their own suites the same way.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from evalforge_core.types import Message, ModelResponse


class FakeModelClient:
    """A scripted `ModelClient`.

    Responses may be a fixed dict, a list consumed in order, or a callable receiving
    the rendered messages — enough to simulate a well-behaved judge, a flaky one, a
    refusing one, or one that has been successfully injected.
    """

    def __init__(
        self,
        responses: dict[str, Any]
        | Sequence[Any]
        | Callable[[Sequence[Message]], Any]
        | None = None,
        *,
        cost_per_call: Decimal = Decimal("0.001"),
        latency_ms: int = 5,
        fail_times: int = 0,
        exception: Exception | None = None,
        echo_canary: bool = True,
    ) -> None:
        self._responses = responses if responses is not None else {"reasoning": "ok", "score": 5}
        self.cost_per_call = cost_per_call
        self.latency_ms = latency_ms
        self.fail_times = fail_times
        self.exception = exception or TimeoutError("simulated provider timeout")
        self.echo_canary = echo_canary

        self.calls: list[dict[str, Any]] = []
        self._index = 0

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        seed: int | None = None,
        max_tokens: int | None = None,  # noqa: ARG002 — mirrors the protocol
        timeout: float = 60.0,  # noqa: ASYNC109,ARG002 — mirrors the protocol
    ) -> ModelResponse:
        self.calls.append(
            {
                "model": model,
                "messages": [(m.role, m.content) for m in messages],
                "schema": response_schema,
                "temperature": temperature,
                "seed": seed,
            }
        )

        if len(self.calls) <= self.fail_times:
            raise self.exception

        payload = self._next(messages)
        if isinstance(payload, dict) and self.echo_canary:
            payload = {**payload, "canary": _canary_from(messages)}

        content = payload if isinstance(payload, str) else json.dumps(payload)
        return ModelResponse(
            content=content,
            model=model,
            prompt_tokens=100,
            completion_tokens=20,
            cost=self.cost_per_call,
            latency_ms=self.latency_ms,
            parsed=payload if isinstance(payload, dict) else None,
        )

    def _next(self, messages: Sequence[Message]) -> Any:
        if callable(self._responses):
            return self._responses(messages)
        if isinstance(self._responses, dict):
            return self._responses
        index = min(self._index, len(self._responses) - 1)
        self._index += 1
        return self._responses[index]

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def last_prompt(self) -> str:
        return "\n".join(content for _, content in self.calls[-1]["messages"])


def _canary_from(messages: Sequence[Message]) -> str:
    """Recover the canary the judge was asked to echo."""
    for message in messages:
        if message.role != "system":
            continue
        marker = 'exactly "'
        start = message.content.find(marker)
        if start != -1:
            start += len(marker)
            end = message.content.find('"', start)
            return message.content[start:end]
    return ""
