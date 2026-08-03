"""The suite YAML schema.

Versioned by `apiVersion` from the first release. Suite files live in users' repos
and must not break on upgrade, which costs nothing to promise now and is expensive
to retrofit later (docs/OPEN_QUESTIONS.md Q11).

`extra="forbid"` throughout: a typo'd key that is silently ignored produces a suite
which looks configured and is not. `capture_args: true` misspelled as `capture_arg`
should be an error, not a shrug.
"""

from __future__ import annotations

import difflib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

API_VERSION = "evalforge.dev/v1"

ReportFormat = Literal["terminal", "json"]

EVALUATOR_TYPES = (
    "exact_match",
    "json_schema",
    "regex",
    "contains",
    "length",
    "numeric_range",
    "set_comparison",
    "llm_judge",
    "trajectory",
    "operational",
    "classification",
    "ranking",
    "calibration",
)


class DatasetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    version: str = "latest-locked"
    path: str | None = Field(
        default=None, description="Local JSONL/CSV file; mutually exclusive with `name`"
    )
    split: str | None = None
    limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_source(self) -> DatasetRef:
        if bool(self.name) == bool(self.path):
            msg = "dataset needs exactly one of `name` (server) or `path` (local file)"
            raise ValueError(msg)
        return self

    @property
    def is_local(self) -> bool:
        return self.path is not None


class TaskRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entrypoint: str = Field(pattern=r"^[\w.]+:[\w.]+$", description="module:function")
    timeout_s: float = Field(default=120.0, gt=0)
    retries: int = Field(default=2, ge=0)


class Execution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concurrency: int = Field(default=8, ge=1, le=256)
    judge_concurrency: int = Field(default=4, ge=1, le=64)
    max_error_rate: float = Field(default=0.10, ge=0, le=1)
    seed: int = 42
    slice_by: list[str] = Field(default_factory=list)
    max_cost: float | None = Field(default=None, gt=0)


class Scale(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: int = 1
    max: int = 5
    normalize: bool = True


class Calibration(BaseModel):
    """One judge's calibration: which labelled set, and what it has to achieve.

    Thresholds live with the evaluator rather than with the gate because they are a
    property of the measurement — an unsubscribe judge needs a tighter false-pass rate
    than a tone judge, whatever gate set happens to reference it. Whether falling short
    *blocks* is the gate set's decision (`calibration.require`).
    """

    model_config = ConfigDict(extra="forbid")

    #: Path to the labelled JSONL, relative to the suite file.
    dataset: str
    version: str = "latest-locked"
    #: Labels that count as "passing", which is what makes the false-pass and false-fail
    #: rates computable. Without it those two numbers are unmeasured, and they are the
    #: ones that matter most.
    passing_labels: list[str] = Field(default_factory=list)
    min_agreement: float | None = Field(default=None, ge=0, le=1)
    min_kappa: float | None = Field(default=None, ge=-1, le=1)
    max_false_pass_rate: float | None = Field(default=None, ge=0, le=1)
    max_false_fail_rate: float | None = Field(default=None, ge=0, le=1)
    min_examples: int | None = Field(default=None, ge=1)
    min_per_class: int | None = Field(default=None, ge=1)
    allow_position_bias: bool = False
    required: bool = False


class CalibrationPolicy(BaseModel):
    """Suite-level calibration settings.

    `require` is the enforcement switch: `false` means an uncalibrated gated judge only
    warns, `true` means it fails the run, and a mapping overrides the thresholds that
    apply when an evaluator does not state its own.
    """

    model_config = ConfigDict(extra="forbid")

    #: Where calibration records live, relative to the suite file. They are committed to
    #: git so `require` works in CI with no server — see calibration_store.py.
    directory: str = "calibrations"
    require: bool | dict[str, Any] = False


class EvaluatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: str

    # Deterministic
    field: str | None = None
    expected_field: str | None = None
    normalize: Literal["none", "case", "whitespace", "punctuation", "all"] = "none"
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    schema_path: str | None = None
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    substrings: list[str] = Field(default_factory=list)
    mode: str | None = None
    case_sensitive: bool = True
    unit: Literal["chars", "words"] = "chars"
    minimum: float | None = None
    maximum: float | None = None
    inclusive: bool = True

    # Judge
    rubric: str | None = None
    rubric_path: str | None = None
    model: str | None = None
    temperature: float = 0.0
    seed: int | None = 42
    inputs: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    scale: Scale = Field(default_factory=Scale)
    votes: int = 1
    timeout_s: float = 60.0
    max_retries: int = 2
    calibration: Calibration | None = None

    # Trajectory
    policy: str | None = None

    # Statistical / operational
    prediction_field: str | None = None
    label_field: str | None = None
    averaging: Literal["macro", "micro", "weighted"] = "macro"
    k: int = 10
    ranking_field: str | None = None
    relevant_field: str | None = None
    confidence_field: str | None = None
    percentiles: list[int] = Field(default_factory=lambda: [50, 95, 99])

    @model_validator(mode="after")
    def _known_type(self) -> EvaluatorSpec:
        if self.type not in EVALUATOR_TYPES:
            closest = _closest(self.type, EVALUATOR_TYPES)
            hint = f" Did you mean {closest!r}?" if closest else ""
            msg = (
                f"evaluator {self.name!r} has unknown type {self.type!r}.{hint} "
                f"Valid types: {', '.join(sorted(EVALUATOR_TYPES))}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _judges_declare_inputs(self) -> EvaluatorSpec:
        if self.type != "llm_judge":
            return self
        if not self.inputs:
            # A judge handed the whole example can read `expected` and grade against
            # the answer key. Enumerating inputs is the only thing that prevents it.
            msg = (
                f"judge {self.name!r} must declare `inputs`: without it the judge can see "
                "`expected` and grade against the answer key"
            )
            raise ValueError(msg)
        if not self.rubric and not self.rubric_path:
            msg = f"judge {self.name!r} needs `rubric` or `rubric_path`"
            raise ValueError(msg)
        if not self.model:
            # Unpinned means the provider can change the model underneath you and
            # invalidate every historical number without any signal.
            msg = (
                f"judge {self.name!r} must pin a `model`: an unpinned judge silently "
                "invalidates every historical comparison when the provider updates it"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _trajectory_needs_a_policy(self) -> EvaluatorSpec:
        if self.type == "trajectory" and not self.policy:
            msg = f"trajectory evaluator {self.name!r} needs a `policy` path"
            raise ValueError(msg)
        return self


class GateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: float | None = None
    maximum: float | None = None
    max_regression: float | None = None
    max_relative_regression: float | None = None
    blocking: bool = True
    slice: dict[str, str] | None = None
    require_baseline: bool = False

    @model_validator(mode="after")
    def _has_a_condition(self) -> GateSpec:
        if all(
            v is None
            for v in (
                self.minimum,
                self.maximum,
                self.max_regression,
                self.max_relative_regression,
            )
        ):
            msg = "declares no condition; a gate that cannot fail gives false assurance"
            raise ValueError(msg)
        return self


class BaselineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["latest_on_branch", "run_id", "none"] = "latest_on_branch"
    branch: str = "main"
    run_id: str | None = None
    require_dataset_match: bool = True

    @model_validator(mode="after")
    def _run_id_present(self) -> BaselineSpec:
        if self.strategy == "run_id" and not self.run_id:
            msg = "baseline strategy 'run_id' needs a `run_id`"
            raise ValueError(msg)
        return self


class ReportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formats: list[ReportFormat] = Field(
        default_factory=lambda: ["terminal", "json"]  # type: ignore[arg-type]
    )
    output: str = "evalforge-report.json"


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    apiVersion: str = API_VERSION  # noqa: N815 — YAML field name
    kind: Literal["EvalSuite"] = "EvalSuite"
    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    description: str | None = None
    extends: str | None = None

    dataset: DatasetRef
    task: TaskRef | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)
    execution: Execution = Field(default_factory=Execution)
    evaluators: list[EvaluatorSpec] = Field(min_length=1)
    gates: dict[str, GateSpec] = Field(default_factory=dict)
    calibration: CalibrationPolicy = Field(default_factory=CalibrationPolicy)
    baseline: BaselineSpec = Field(default_factory=BaselineSpec)
    report: ReportSpec = Field(default_factory=ReportSpec)

    @model_validator(mode="after")
    def _supported_api_version(self) -> Suite:
        if self.apiVersion != API_VERSION:
            msg = (
                f"unsupported apiVersion {self.apiVersion!r}; this CLI understands {API_VERSION!r}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _unique_evaluator_names(self) -> Suite:
        seen: set[str] = set()
        for evaluator in self.evaluators:
            if evaluator.name in seen:
                msg = f"duplicate evaluator name {evaluator.name!r}"
                raise ValueError(msg)
            seen.add(evaluator.name)
        return self

    @property
    def judge_ratio(self) -> float:
        if not self.evaluators:
            return 0.0
        judges = sum(1 for e in self.evaluators if e.type == "llm_judge")
        return judges / len(self.evaluators)


def _closest(value: str, candidates: tuple[str, ...]) -> str | None:
    matches = difflib.get_close_matches(value, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None
