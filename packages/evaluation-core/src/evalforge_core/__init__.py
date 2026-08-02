"""EvalForge evaluation engine (pure library — no I/O).

No HTTP, no database, no provider SDKs. Model access arrives through an injected
`ModelClient` protocol. That boundary is what makes local mode, CI mode, and server
mode the same code path, and it is enforced in CI by `.importlinter`.
"""

from evalforge_core.aggregate import aggregate_scores, scores_for
from evalforge_core.compare import Comparison, ExampleRegression, compare_metrics
from evalforge_core.dataset import Dataset
from evalforge_core.gates import GateReport, evaluate_gates
from evalforge_core.paths import PathError, resolve, resolve_in_context
from evalforge_core.runner import EvalResult, RunConfig, run_suite
from evalforge_core.suite import EvalSuite, FunctionEvaluator, evaluate
from evalforge_core.types import (
    CorpusEvaluator,
    EvalContext,
    Evaluator,
    EvaluatorBase,
    Message,
    ModelClient,
    ModelResponse,
    Task,
)

__version__ = "0.1.0.dev0"

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
