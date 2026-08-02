"""Operational evaluators — latency, cost, tokens, error rate, tool usage.

Computed entirely from what the run already recorded. Zero model calls, zero cost,
and available on every example. Percentiles are why these are corpus-level: a p95
cannot be recovered from per-example means.

These belong in a suite for the same reason a benchmark reports memory alongside
speed. A change that improves groundedness by 2% while tripling cost per email is
not obviously an improvement, and the report should force that trade-off into view.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from evalforge_core.stats import mean, percentile
from evalforge_types import ExampleResult, Metric, ResultStatus, SpanType


class OperationalEvaluator:
    """Emits the standard operational metric family for a run."""

    name = "operational"
    version = 1

    def __init__(
        self, *, percentiles: Sequence[int] = (50, 95, 99), name: str | None = None
    ) -> None:
        if name:
            self.name = name
        self.percentiles = list(percentiles)

    def evaluate_corpus(self, results: Sequence[ExampleResult]) -> list[Metric]:
        if not results:
            return []

        n = len(results)
        latencies = [float(r.latency_ms) for r in results if r.ok]
        metrics: list[Metric] = []

        for q in self.percentiles:
            metrics.append(
                Metric(
                    key=f"p{q}_latency_ms",
                    value=percentile(latencies, q) if latencies else 0.0,
                    count=len(latencies),
                    unit="ms",
                    aggregation=f"p{q}",
                )
            )

        if latencies:
            metrics.append(
                Metric(
                    key="mean_latency_ms", value=mean(latencies), count=len(latencies), unit="ms"
                )
            )

        # Cost is Decimal end to end and only becomes a float at the reporting
        # boundary. Accumulating money in binary floating point is how totals drift.
        total_cost = sum((r.total_cost for r in results), Decimal(0))
        metrics += [
            Metric(key="total_cost", value=float(total_cost), count=n, unit="usd"),
            Metric(key="cost_per_example", value=float(total_cost) / n, count=n, unit="usd"),
            Metric(
                key="judge_cost",
                value=float(sum((s.cost for r in results for s in r.scores), Decimal(0))),
                count=n,
                unit="usd",
            ),
            Metric(
                key="total_tokens",
                value=float(sum(r.tokens for r in results)),
                count=n,
                unit="count",
            ),
            Metric(
                key="error_rate",
                value=sum(1 for r in results if not r.ok) / n,
                count=n,
                unit="ratio",
            ),
            Metric(
                key="timeout_rate",
                value=sum(1 for r in results if r.status is ResultStatus.TIMEOUT) / n,
                count=n,
                unit="ratio",
            ),
            Metric(
                key="retry_count",
                value=float(sum(r.retry_count for r in results)),
                count=n,
                unit="count",
            ),
        ]

        metrics += self._trace_metrics(results, n)
        return metrics

    def _trace_metrics(self, results: Sequence[ExampleResult], n: int) -> list[Metric]:
        traced = [r for r in results if r.trace is not None]
        if not traced:
            return []

        tool_calls = 0
        model_calls = 0
        spans = 0
        for result in traced:
            assert result.trace is not None
            spans += len(result.trace.spans)
            tool_calls += len(result.trace.spans_by_type(SpanType.TOOL))
            model_calls += len(result.trace.spans_by_type(SpanType.LLM))

        return [
            Metric(key="tool_calls_per_example", value=tool_calls / len(traced), count=len(traced)),
            Metric(
                key="model_calls_per_example", value=model_calls / len(traced), count=len(traced)
            ),
            Metric(key="spans_per_example", value=spans / len(traced), count=len(traced)),
            Metric(
                key="trace_coverage",
                value=len(traced) / n,
                count=n,
                unit="ratio",
            ),
        ]
