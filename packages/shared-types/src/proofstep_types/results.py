"""Per-example execution results."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from proofstep_types.common import ResultStatus
from proofstep_types.score import Score
from proofstep_types.trace import Trace


class TaskError(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str
    message: str
    traceback: str | None = None


class ExampleResult(BaseModel):
    """The outcome of running the task against one example, with its scores.

    Serialized one-per-line into the run journal as each example completes, so a
    crash at example 190/200 loses nothing and `--resume` can skip what finished
    (docs/EVALUATION_ENGINE.md §4).
    """

    example_id: str
    status: ResultStatus = ResultStatus.OK
    output: Any = None
    scores: list[Score] = Field(default_factory=list)
    trace: Trace | None = None

    latency_ms: int = 0
    cost: Decimal = Decimal(0)
    tokens: int = 0
    retry_count: int = 0
    error: TaskError | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None

    # Retained so a comparison can slice by any dimension the dataset carried.
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status is ResultStatus.OK

    @property
    def total_cost(self) -> Decimal:
        """Task cost plus the cost of evaluating it.

        Judge spend is part of what a suite costs to run, and hiding it in a
        separate number is how teams end up surprised by the bill.
        """
        return self.cost + sum((s.cost for s in self.scores), Decimal(0))

    def score_for(self, metric: str) -> Score | None:
        return next((s for s in self.scores if s.metric == metric), None)
