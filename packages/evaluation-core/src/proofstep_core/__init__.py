"""Proofstep evaluation engine (pure library — no I/O).

No HTTP, no database, no provider SDKs. Model access arrives through an injected
`ModelClient` protocol. That boundary is what makes local mode, CI mode, and server
mode the same code path, and it is enforced in CI by `.importlinter`.
"""

from importlib import metadata as _metadata

from proofstep_core.aggregate import aggregate_scores, scores_for
from proofstep_core.compare import Comparison, ExampleRegression, compare_metrics
from proofstep_core.dataset import Dataset
from proofstep_core.gates import GateReport, evaluate_gates
from proofstep_core.paths import PathError, resolve, resolve_in_context
from proofstep_core.runner import EvalResult, RunConfig, run_suite
from proofstep_core.suite import EvalSuite, FunctionEvaluator, evaluate
from proofstep_core.types import (
    CorpusEvaluator,
    EvalContext,
    Evaluator,
    EvaluatorBase,
    Message,
    ModelClient,
    ModelResponse,
    Task,
)

# Read from the installed distribution rather than written here twice. A hand-maintained
# copy drifts the first time a release bumps one and not the other — which it already did,
# reporting 0.1.0.dev0 from a 0.1.0 wheel.
__version__ = _metadata.version("proofstep-core")

__all__ = [
    "Comparison",
    "CorpusEvaluator",
    "Dataset",
    "EvalContext",
    "EvalResult",
    "EvalSuite",
    "Evaluator",
    "EvaluatorBase",
    "ExampleRegression",
    "FunctionEvaluator",
    "GateReport",
    "Message",
    "ModelClient",
    "ModelResponse",
    "PathError",
    "RunConfig",
    "Task",
    "aggregate_scores",
    "compare_metrics",
    "evaluate",
    "evaluate_gates",
    "resolve",
    "resolve_in_context",
    "run_suite",
    "scores_for",
]
