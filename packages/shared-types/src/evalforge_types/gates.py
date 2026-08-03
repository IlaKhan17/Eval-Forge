"""Quality gate rules — the contract between a suite file and the gate engine."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evalforge_types.common import Severity


class GateRule(BaseModel):
    """One threshold applied to one metric.

    ``slice`` is what makes protected-class gating possible. A rule can target
    ``per_class_recall`` for ``unsubscribe`` specifically rather than the macro
    average — which matters because a 3%-prevalence class can collapse from 0.99
    to 0.20 recall while macro accuracy moves 0.3%, passing every aggregate gate
    (docs/EVALUATION_ENGINE.md §7).
    """

    model_config = ConfigDict(frozen=True)

    metric_key: str
    minimum: float | None = None
    maximum: float | None = None
    max_absolute_regression: float | None = None
    max_relative_regression: float | None = None
    severity: Severity = Severity.BLOCK
    slice: dict[str, str] | None = None
    require_baseline: bool = Field(
        default=False,
        description="Fail when no baseline exists, rather than skipping regression checks",
    )
    max_error_rate: float = Field(
        default=0.05,
        description="Above this share of errored evaluations the gate reports ERROR, not PASS",
    )

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCK

    @property
    def full_key(self) -> str:
        if not self.slice:
            return self.metric_key
        inner = ",".join(f"{k}={v}" for k, v in sorted(self.slice.items()))
        return f"{self.metric_key}[{inner}]"

    @property
    def needs_baseline(self) -> bool:
        return self.max_absolute_regression is not None or self.max_relative_regression is not None

    @model_validator(mode="after")
    def _at_least_one_condition(self) -> GateRule:
        if not any(
            v is not None
            for v in (
                self.minimum,
                self.maximum,
                self.max_absolute_regression,
                self.max_relative_regression,
            )
        ):
            msg = (
                f"Gate on {self.metric_key!r} declares no condition; a gate that "
                "cannot fail is a gate that gives false assurance"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _bounds_are_ordered(self) -> GateRule:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            msg = (
                f"Gate on {self.metric_key!r} has minimum {self.minimum} above "
                f"maximum {self.maximum}; no value can satisfy it"
            )
            raise ValueError(msg)
        return self


class GateSet(BaseModel):
    """A named collection of gate rules, versioned with the suite that declares it."""

    model_config = ConfigDict(frozen=True)

    name: str = "default"
    rules: list[GateRule] = Field(default_factory=list)
    require_dataset_match: bool = Field(
        default=True,
        description=(
            "Refuse to gate when candidate and baseline used different dataset "
            "content hashes. Comparing across datasets is a silent source of "
            "confidently wrong conclusions."
        ),
    )
    require_calibration: bool | CalibrationRequirementSpec = Field(
        default=False,
        description=(
            "Turn the uncalibrated-judge warning into a hard error. `true` uses the "
            "recommended thresholds; a mapping overrides them."
        ),
    )

    @property
    def calibration_requirement(self) -> CalibrationRequirementSpec | None:
        """Normalize the bool-or-mapping form into one thing the gate engine reads.

        `None` means no requirement, so an uncalibrated judge warns rather than blocks.
        Warning rather than silence matters: a gate on a judge nobody has checked is
        the failure this whole subsystem exists to make visible.
        """
        if self.require_calibration is False:
            return None
        if self.require_calibration is True:
            return CalibrationRequirementSpec()
        return self.require_calibration

    def rules_for(self, metric_key: str) -> list[GateRule]:
        return [r for r in self.rules if r.metric_key == metric_key]


class CalibrationRequirementSpec(BaseModel):
    """What a gate set demands before it will trust a judge.

    ``require_calibration: true`` in YAML means "these defaults", and a mapping means
    "these defaults with overrides". The defaults are the recommended values for a
    safety-relevant metric (docs/EVALUATION_ENGINE.md §5).

    ``max_false_pass_rate`` is deliberately four times tighter than
    ``max_false_fail_rate``. A judge that passes work a human rejected ships a defect;
    one that fails acceptable work annoys somebody. Treating those as the same error is
    how a gate ends up either useless or bypassed.
    """

    model_config = ConfigDict(frozen=True)

    required: bool = Field(
        default=True,
        description="Fail the run rather than warn when the requirement is unmet",
    )
    min_agreement: float = Field(default=0.8, ge=0, le=1)
    min_kappa: float | None = Field(default=0.6, ge=-1, le=1)
    max_false_pass_rate: float | None = Field(default=0.05, ge=0, le=1)
    max_false_fail_rate: float | None = Field(default=0.20, ge=0, le=1)
    min_examples: int = Field(default=100, ge=1)
    min_per_class: int = Field(default=50, ge=1)
    max_error_rate: float = Field(default=0.05, ge=0, le=1)
    allow_position_bias: bool = False


class CalibrationStatus(BaseModel):
    """What is known about one judge metric's calibration at gate time.

    ``calibrated=False`` and "calibrated but failing" are different states with
    different fixes — go and calibrate it, versus go and fix the judge — so they are
    reported separately rather than folded into one boolean.
    """

    model_config = ConfigDict(frozen=True)

    metric_key: str
    evaluator_name: str = ""
    #: Config hash of the calibrated evaluator version. A judge whose rubric, model, or
    #: parameters changed has a different hash, and an old calibration does not apply to
    #: it — that is the whole point of versioning by config hash.
    evaluator_version_hash: str | None = None
    calibrated: bool = False
    satisfied: bool | None = None
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    n_examples: int = 0
    agreement: float | None = None
    kappa: float | None = None
    false_pass_rate: float | None = None
    at_human_ceiling: bool = False
    calibrated_at: datetime | None = None
    #: Set when a calibration exists but for a different version of the evaluator.
    stale_for_version: str | None = None

    @property
    def is_stale(self) -> bool:
        return self.stale_for_version is not None
