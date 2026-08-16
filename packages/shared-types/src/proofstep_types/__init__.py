"""Shared Pydantic models for Proofstep — the wire and domain contract.

This package is a leaf: it depends on nothing else of ours, which is what lets the
SDK, the pure libraries, the API, and the generated TypeScript types all agree on one
definition. Enforced by the `types-are-leaf` contract in `.importlinter`.
"""

from importlib import metadata as _metadata

from proofstep_types.common import (
    CaptureMode,
    ExitCode,
    OutputKind,
    ResultStatus,
    Severity,
    SpanType,
    Status,
    Verdict,
)
from proofstep_types.dataset import Example, content_hash
from proofstep_types.gates import (
    CalibrationRequirementSpec,
    CalibrationStatus,
    GateRule,
    GateSet,
)
from proofstep_types.results import ExampleResult, TaskError
from proofstep_types.score import GateResult, Metric, MetricDelta, Score
from proofstep_types.trace import Span, SpanEvent, TokenUsage, Trace

# Read from the installed distribution rather than written here twice. A hand-maintained
# copy drifts the first time a release bumps one and not the other — which it already did,
# reporting 0.1.0.dev0 from a 0.1.0 wheel.
__version__ = _metadata.version("proofstep-types")

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
