"""Protocols and context objects — the extension points of the evaluation engine.

Everything here is structural typing. An evaluator is anything with the right shape,
so users are never forced to inherit from our base class, and tests never need a mock
framework. Model access arrives as an injected `ModelClient` protocol rather than a
concrete provider SDK, which is what keeps this package pure and testable offline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from proofstep_types import Example, ExampleResult, Metric, Score, Trace


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A completion, plus the accounting a suite needs to report its own cost."""

    content: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal = Decimal(0)
    latency_ms: int = 0
    parsed: Any = None
    finish_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class ModelClient(Protocol):
    """The only way this package reaches a model.

    Deliberately narrow. A provider SDK's full surface is not needed to score
    something, and depending on one would make the engine untestable without a
    network and unusable against self-hosted endpoints.
    """

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        seed: int | None = None,
        max_tokens: int | None = None,
        timeout: float = 60.0,  # noqa: ASYNC109 — provider deadline, not a cancel scope
    ) -> ModelResponse: ...


@dataclass(slots=True)
class EvalContext:
    """Everything an evaluator is allowed to see about one example."""

    example: Example
    output: Any
    expected: dict[str, Any] | None = None
    trace: Trace | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    models: ModelClient | None = None
    seed: int | None = None

    @property
    def has_trace(self) -> bool:
        return self.trace is not None


@runtime_checkable
class Task(Protocol):
    """The user's code under evaluation."""

    async def __call__(self, example: Example) -> Any: ...


@runtime_checkable
class Evaluator(Protocol):
    """Scores a single example.

    Returning a list lets one evaluator emit several metrics — a classification
    evaluator emits macro-F1 alongside per-class recall — without needing a second
    abstraction for the multi-output case.
    """

    name: str
    version: int

    async def evaluate(self, ctx: EvalContext) -> Score | list[Score]: ...


@runtime_checkable
class CorpusEvaluator(Protocol):
    """Scores the whole result set at once.

    Some metrics are not means of per-example scores. F1 needs the full confusion
    matrix; NDCG needs the full ranking; percentiles need the full distribution.
    Treating F1 as "the mean of per-example F1" is a real and common statistical
    error, so the engine models corpus metrics as a distinct stage rather than
    forcing them through per-example aggregation.
    """

    name: str
    version: int

    def evaluate_corpus(self, results: Sequence[ExampleResult]) -> list[Metric]: ...


class EvaluatorBase:
    """Optional convenience base. Implementing the protocol directly is equally valid."""

    name: str = "unnamed"
    version: int = 1
    requires_expected: bool = False
    requires_trace: bool = False

    def __init__(self, *, name: str | None = None, version: int | None = None) -> None:
        if name is not None:
            self.name = name
        if version is not None:
            self.version = version

    async def evaluate(self, ctx: EvalContext) -> Score | list[Score]:  # pragma: no cover
        raise NotImplementedError

    def _precondition_failure(self, ctx: EvalContext) -> Score | None:
        """Return an errored score when the evaluator cannot meaningfully run.

        Note this produces an *error*, never a zero. An evaluator that needed
        ground truth and did not get it has failed to measure, which is a different
        fact from measuring a failure, and the aggregation layer treats it as such.
        """
        if self.requires_expected and ctx.expected is None:
            return Score.failure(
                self.name,
                f"{self.name} requires an expected value but the example has none",
            )
        if self.requires_trace and ctx.trace is None:
            return Score.failure(
                self.name,
                f"{self.name} requires a captured trace but none was recorded; "
                "is the task instrumented?",
            )
        return None
