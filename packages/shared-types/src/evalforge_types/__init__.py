"""Shared Pydantic models for EvalForge — the wire and domain contract.

This package is a leaf: it depends on nothing else of ours, which is what lets the
SDK, the pure libraries, the API, and the generated TypeScript types all agree on one
definition. Enforced by the `types-are-leaf` contract in `.importlinter`.
"""

from evalforge_types.common import (
    CaptureMode,
    ExitCode,
    OutputKind,
    ResultStatus,
    Severity,
    SpanType,
    Status,
    Verdict,
)
from evalforge_types.dataset import Example, content_hash
from evalforge_types.gates import (
    CalibrationRequirementSpec,
    CalibrationStatus,
    GateRule,
    GateSet,
)
from evalforge_types.results import ExampleResult, TaskError
from evalforge_types.score import GateResult, Metric, MetricDelta, Score
from evalforge_types.trace import Span, SpanEvent, TokenUsage, Trace

__version__ = "0.1.0.dev0"

__all__ = [
    "CalibrationRequirementSpec",
    "CalibrationStatus",
    "CaptureMode",
    "Example",
    "ExampleResult",
    "ExitCode",
    "GateResult",
    "GateRule",
    "GateSet",
    "Metric",
    "MetricDelta",
    "OutputKind",
    "ResultStatus",
    "Score",
    "Severity",
    "Span",
    "SpanEvent",
    "SpanType",
    "Status",
    "TaskError",
    "TokenUsage",
    "Trace",
    "Verdict",
    "content_hash",
]
