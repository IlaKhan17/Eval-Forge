"""Built-in evaluators.

Reach for a judge only when the property is genuinely subjective. Everything in
`deterministic` and `statistical` is free, instant, and exactly reproducible.
"""

from proofstep_core.evaluators.deterministic import (
    Contains,
    ExactMatch,
    JsonSchemaMatch,
    LengthWithin,
    NumericRange,
    RegexMatch,
    SetComparison,
)
from proofstep_core.evaluators.judge import LLMJudge
from proofstep_core.evaluators.operational import OperationalEvaluator
from proofstep_core.evaluators.statistical import (
    CalibrationEvaluator,
    ClassificationEvaluator,
    DiscriminationEvaluator,
    RankingEvaluator,
    confusion_matrix,
    expected_calibration_error,
    ndcg_at_k,
    reciprocal_rank,
)

__all__ = [
    "CalibrationEvaluator",
    "ClassificationEvaluator",
    "Contains",
    "DiscriminationEvaluator",
    "ExactMatch",
    "JsonSchemaMatch",
    "LLMJudge",
    "LengthWithin",
    "NumericRange",
    "OperationalEvaluator",
    "RankingEvaluator",
    "RegexMatch",
    "SetComparison",
    "confusion_matrix",
    "expected_calibration_error",
    "ndcg_at_k",
    "reciprocal_rank",
]
