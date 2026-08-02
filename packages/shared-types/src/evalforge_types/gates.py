"""Quality gate rules — the contract between a suite file and the gate engine."""

from __future__ import annotations

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
    require_calibration: bool = Field(
        default=False,
        description="Turn the uncalibrated-judge warning into a hard error",
    )

    def rules_for(self, metric_key: str) -> list[GateRule]:
        return [r for r in self.rules if r.metric_key == metric_key]
