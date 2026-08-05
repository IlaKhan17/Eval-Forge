"""Build evaluator instances from suite specs.

The registry lives in the CLI rather than in `evaluation-core`, because it is
config-driven: it turns YAML into objects. Keeping it out of the core is what lets
the core stay a pure library with no notion of a file format.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from evalforge_core.evaluators import (
    CalibrationEvaluator,
    ClassificationEvaluator,
    Contains,
    DiscriminationEvaluator,
    ExactMatch,
    JsonSchemaMatch,
    LengthWithin,
    LLMJudge,
    NumericRange,
    OperationalEvaluator,
    RankingEvaluator,
    RegexMatch,
    SetComparison,
)
from evalforge_trajectory.evaluator import TrajectoryEvaluator

if TYPE_CHECKING:
    from evalforge_cli.suite.loader import LoadedSuite
    from evalforge_cli.suite.schema import EvaluatorSpec

CORPUS_TYPES = frozenset(
    {"classification", "ranking", "calibration", "discrimination", "operational"}
)


class RegistryError(ValueError):
    """An evaluator spec could not be turned into an evaluator."""


def build_evaluators(loaded: LoadedSuite) -> tuple[list[Any], list[Any]]:
    """Return (per-example evaluators, corpus evaluators).

    The split is not cosmetic. Corpus metrics — F1, NDCG, percentiles — are not
    means of per-example scores, and running them through per-example aggregation
    would produce plausible, wrong numbers.
    """
    per_example: list[Any] = []
    corpus: list[Any] = []

    for spec in loaded.suite.evaluators:
        built = _build_one(spec, loaded)
        if spec.type in CORPUS_TYPES:
            corpus.append(built)
        else:
            per_example.append(built)
    return per_example, corpus


def _build_one(spec: EvaluatorSpec, loaded: LoadedSuite) -> Any:  # noqa: PLR0911, PLR0912
    field = spec.field or "output"

    if spec.type == "exact_match":
        return ExactMatch(
            name=spec.name,
            field=field,
            expected_field=spec.expected_field,
            normalize=spec.normalize,
        )

    if spec.type == "json_schema":
        return JsonSchemaMatch(_load_schema(spec, loaded), name=spec.name, field=field)

    if spec.type == "regex":
        return RegexMatch(name=spec.name, field=field, allow=spec.allow, deny=spec.deny)

    if spec.type == "contains":
        return Contains(
            spec.substrings,
            name=spec.name,
            field=field,
            mode=spec.mode or "all",  # type: ignore[arg-type]
            case_sensitive=spec.case_sensitive,
        )

    if spec.type == "length":
        return LengthWithin(
            name=spec.name,
            field=field,
            minimum=int(spec.minimum) if spec.minimum is not None else None,
            maximum=int(spec.maximum) if spec.maximum is not None else None,
            unit=spec.unit,
        )

    if spec.type == "numeric_range":
        return NumericRange(
            name=spec.name,
            field=field,
            minimum=spec.minimum,
            maximum=spec.maximum,
            inclusive=spec.inclusive,
        )

    if spec.type == "set_comparison":
        return SetComparison(
            name=spec.name,
            field=field,
            expected_field=spec.expected_field,
            mode=spec.mode or "equals",  # type: ignore[arg-type]
        )

    if spec.type == "llm_judge":
        assert spec.model is not None  # guaranteed by schema validation
        return LLMJudge(
            name=spec.name,
            rubric=_load_rubric(spec, loaded),
            model=spec.model,
            inputs=spec.inputs,
            mode="classify" if spec.labels else "rubric",
            labels=spec.labels or None,
            passing_labels=_passing_labels(spec),
            scale=(spec.scale.min, spec.scale.max),
            normalize=spec.scale.normalize,
            temperature=spec.temperature,
            seed=spec.seed,
            votes=spec.votes,
            timeout_s=spec.timeout_s,
            max_retries=spec.max_retries,
        )

    if spec.type == "trajectory":
        assert spec.policy is not None
        return TrajectoryEvaluator(loaded.resolve_path(spec.policy), name=spec.name)

    if spec.type == "classification":
        return ClassificationEvaluator(
            name=spec.name,
            prediction_field=spec.prediction_field or "intent",
            label_field=spec.label_field,
            averaging=spec.averaging,
            labels=spec.labels or None,
        )

    if spec.type == "ranking":
        return RankingEvaluator(
            name=spec.name,
            k=spec.k,
            ranking_field=spec.ranking_field or "results",
            relevant_field=spec.relevant_field or "relevant",
        )

    if spec.type == "discrimination":
        return DiscriminationEvaluator(
            name=spec.name,
            score_field=spec.prediction_field or "predicted",
            outcome_field=spec.label_field or "correct",
        )

    if spec.type == "calibration":
        return CalibrationEvaluator(
            correct_field=spec.correct_field,
            name=spec.name,
            confidence_field=spec.confidence_field or "confidence",
            prediction_field=spec.prediction_field or "intent",
            label_field=spec.label_field or "intent",
        )

    if spec.type == "operational":
        return OperationalEvaluator(name=spec.name, percentiles=spec.percentiles)

    msg = f"no builder for evaluator type {spec.type!r}"
    raise RegistryError(msg)


def _load_schema(spec: EvaluatorSpec, loaded: LoadedSuite) -> dict[str, Any]:
    if spec.schema_ is not None:
        return spec.schema_
    if spec.schema_path is None:
        msg = f"evaluator {spec.name!r} needs `schema` or `schema_path`"
        raise RegistryError(msg)
    path = loaded.resolve_path(spec.schema_path)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"evaluator {spec.name!r}: cannot read schema {path}: {exc}"
        raise RegistryError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"evaluator {spec.name!r}: schema at {path} is not a JSON object"
        raise RegistryError(msg)
    return parsed


def _passing_labels(spec: EvaluatorSpec) -> list[str] | None:
    """The judge's passing labels, from the evaluator or its calibration block.

    Two places because a suite that only calibrates does not need the evaluator-level field, and
    one that only gates does not need the calibration block. Preferring the evaluator keeps the
    scoring definition next to the scoring.
    """
    if spec.passing_labels:
        return spec.passing_labels
    if spec.calibration and spec.calibration.passing_labels:
        return list(spec.calibration.passing_labels)
    return None


def load_rubric_text(spec: EvaluatorSpec, loaded: LoadedSuite) -> str:
    """The rubric as text, wherever it came from.

    Public because the evaluator's version hash is computed over the rubric *text*, not
    its path: editing `rubrics/groundedness.md` in place redefines the metric, and a hash
    over the path would leave the old calibration blessing a different ruler.
    """
    return _load_rubric(spec, loaded)


def _load_rubric(spec: EvaluatorSpec, loaded: LoadedSuite) -> str:
    if spec.rubric:
        return spec.rubric
    assert spec.rubric_path is not None
    return loaded.resolve_path(spec.rubric_path).read_text(encoding="utf-8")


def estimate_judge_calls(loaded: LoadedSuite, example_count: int) -> int:
    """How many model calls a run will make, for `--dry-run`.

    Being able to see the cost before paying it is the point: a suite can be
    expensive, and discovering that after the fact is a bad way to learn.
    """
    return sum(
        example_count * spec.votes for spec in loaded.suite.evaluators if spec.type == "llm_judge"
    )
