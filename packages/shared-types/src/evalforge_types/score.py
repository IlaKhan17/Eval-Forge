"""Scores and metrics — what an evaluator produces and what a gate consumes."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evalforge_types.common import Severity


class Score(BaseModel):
    """The result of one evaluator applied to one example.

    The single most important invariant in this model: ``error`` is not ``value=0``.
    A judge that timed out is not a failing example. Aggregation excludes errored
    scores from the mean and counts them separately, and a metric with too high an
    error rate gates as ERROR rather than PASS. Silently scoring infrastructure
    failures as zero is the fastest way to make a gate untrustworthy
    (docs/EVALUATION_ENGINE.md §1).
    """

    model_config = ConfigDict(frozen=True)

    metric: str = Field(description="Metric key this score contributes to")
    value: float | None = Field(
        default=None, description="Normalized to [0,1] where meaningful; None if not scalar"
    )
    passed: bool | None = Field(default=None, description="Binary verdict, if applicable")
    label: str | None = Field(default=None, description="Categorical evaluators")
    raw: Any = Field(default=None, description="Non-scalar payload (matrix, list, …)")
    reasoning: str | None = Field(default=None, description="Judge rationale, if any")
    confidence: float | None = None
    cost: Decimal = Decimal(0)
    latency_ms: int = 0
    error: str | None = Field(
        default=None, description="The evaluator itself failed — never conflate with value=0"
    )
    slice: dict[str, str] | None = Field(
        default=None, description="Slice this score belongs to, e.g. {'class': 'unsubscribe'}"
    )

    @property
    def errored(self) -> bool:
        return self.error is not None

    @property
    def counts_toward_mean(self) -> bool:
        return self.error is None and self.value is not None

    @model_validator(mode="after")
    def _check_error_has_no_value(self) -> Score:
        if self.error is not None and self.value is not None:
            msg = (
                f"Score for {self.metric!r} has both error and value; an errored "
                "evaluation must not contribute a score"
            )
            raise ValueError(msg)
        return self

    @classmethod
    def failure(cls, metric: str, error: str, *, latency_ms: int = 0) -> Score:
        """Construct an errored score. Prefer this over `Score(value=0.0)`."""
        return cls(metric=metric, error=error, latency_ms=latency_ms)

    @classmethod
    def binary(cls, metric: str, passed: bool, **kw: Any) -> Score:
        return cls(metric=metric, value=1.0 if passed else 0.0, passed=passed, **kw)


class Metric(BaseModel):
    """An aggregate over many scores — one row of a comparison table."""

    model_config = ConfigDict(frozen=True)

    key: str
    value: float
    count: int = Field(description="Number of scores that contributed")
    error_count: int = Field(default=0, description="Scores excluded because they errored")
    stddev: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    unit: str | None = Field(default=None, description="ms, usd, ratio, count, …")
    slice: dict[str, str] | None = None
    aggregation: str = "mean"

    @property
    def full_key(self) -> str:
        """Key including its slice, e.g. ``per_class_recall[class=unsubscribe]``."""
        if not self.slice:
            return self.key
        inner = ",".join(f"{k}={v}" for k, v in sorted(self.slice.items()))
        return f"{self.key}[{inner}]"

    @property
    def error_rate(self) -> float:
        total = self.count + self.error_count
        return self.error_count / total if total else 0.0


class MetricDelta(BaseModel):
    """A candidate metric compared against a baseline."""

    model_config = ConfigDict(frozen=True)

    key: str
    slice: dict[str, str] | None = None
    baseline: float | None
    candidate: float | None
    absolute_delta: float | None = None
    relative_delta: float | None = None
    count: int = 0
    ci_low: float | None = None
    ci_high: float | None = None
    significant: bool | None = Field(
        default=None,
        description=(
            "Bootstrap CI on the delta excludes zero. Advisory only — gates use "
            "thresholds, because at n=200 most real regressions are not significant "
            "and gating on p-values would let them through."
        ),
    )

    @property
    def full_key(self) -> str:
        if not self.slice:
            return self.key
        inner = ",".join(f"{k}={v}" for k, v in sorted(self.slice.items()))
        return f"{self.key}[{inner}]"


class GateResult(BaseModel):
    """The verdict of one gate rule against one metric."""

    model_config = ConfigDict(frozen=True)

    metric_key: str
    slice: dict[str, str] | None = None
    verdict: str
    severity: Severity = Severity.BLOCK
    rule: str | None = Field(default=None, description="Which clause fired: minimum, maximum, …")
    threshold: float | None = None
    actual: float | None = None
    baseline: float | None = None
    message: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCK
