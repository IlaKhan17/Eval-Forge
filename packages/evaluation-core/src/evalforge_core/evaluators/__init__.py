"""Built-in evaluators.

Reach for a judge only when the property is genuinely subjective. Everything in
`deterministic` and `statistical` is free, instant, and exactly reproducible.
"""

from evalforge_core.evaluators.deterministic import (
    Contains,
    ExactMatch,
    JsonSchemaMatch,
    LengthWithin,
    NumericRange,
    RegexMatch,
    SetComparison,
)
from evalforge_core.evaluators.judge import LLMJudge
from evalforge_core.evaluators.operational import OperationalEvaluator
from evalforge_core.evaluators.statistical import (
    CalibrationEvaluator,
    ClassificationEvaluator,
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
