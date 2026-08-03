"""Datasets, evaluators, experiments, gates, and policies.

The theme of this schema is **immutability at the boundaries**. Reproducibility is
the product's central claim, and a claim enforced by convention is not enforced at
all — so it is enforced by the schema:

- A locked dataset version cannot be modified. Checked in the application, backed by
  a database trigger, and *proved* by a content hash stored on lock. Without the
  hash, a comparison across mutated data looks completely normal and yields a
  confidently wrong conclusion.
- An evaluator version is immutable and keyed by the hash of everything that could
  change a score, including the judge model and temperature. A judge whose model
  silently upgrades underneath you is a reproducibility bug, and pinning it here is
  the fix.
- Experiment results are append-only. History that can be rewritten is not history.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from evalforge_api.db.base import IdentifiedBase, SoftDeleteMixin, TimestampMixin

DATASET_KINDS = ("golden", "synthetic", "adversarial", "calibration", "general")
VERSION_STATUSES = ("draft", "locked")
EVALUATOR_TYPES = (
    "exact_match",
    "json_schema",
    "regex",
    "contains",
    "length",
    "numeric_range",
    "set_comparison",
    "custom_python",
    "business_rule",
    "statistical",
    "embedding_similarity",
    "llm_judge",
    "trajectory",
    "security",
    "operational",
)
OUTPUT_KINDS = ("binary", "score", "categorical", "numeric")
RUN_STATUSES = ("pending", "running", "succeeded", "failed", "cancelled", "partial")
RUN_TRIGGERS = ("cli", "ci", "ui", "schedule")
RESULT_STATUSES = ("ok", "error", "timeout", "skipped")
SEVERITIES = ("block", "warn")


# --------------------------------------------------------------------- datasets


class Dataset(IdentifiedBase, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "datasets"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    kind: Mapped[str] = mapped_column(String(20), default="general")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    versions: Mapped[list[DatasetVersion]] = relationship(back_populates="dataset")

    __table_args__ = (
        Index(
            "uq_datasets_project_slug_active",
            "project_id",
            "slug",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
        CheckConstraint(f"kind IN {DATASET_KINDS}", name="kind_valid"),
    )


class DatasetVersion(IdentifiedBase, TimestampMixin):
    """Immutable once locked."""

    __tablename__ = "dataset_versions"

    project_id: Mapped[uuid.UUID] = mapped_column()
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    version: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="draft")

    # SHA-256 over the canonically serialized, ordered examples. Recorded on lock so
    # a later run can *prove* it used identical data rather than merely claiming the
    # same version label.
    content_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), default=None)
    example_count: Mapped[int] = mapped_column(Integer, default=0)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="SET NULL"), default=None
    )
    split: Mapped[str | None] = mapped_column(String(20), default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    dataset: Mapped[Dataset] = relationship(back_populates="versions")
    examples: Mapped[list[DatasetExample]] = relationship(back_populates="version")

    __table_args__ = (
        UniqueConstraint("dataset_id", "version", name="uq_dataset_versions_label"),
        CheckConstraint(f"status IN {VERSION_STATUSES}", name="status_valid"),
        # A locked version without a hash would defeat the entire purpose.
        CheckConstraint("status = 'draft' OR content_hash IS NOT NULL", name="locked_has_hash"),
        Index("ix_dataset_versions_project_dataset", "project_id", "dataset_id", "created_at"),
    )

    @property
    def is_locked(self) -> bool:
        return self.status == "locked"


class DatasetExample(IdentifiedBase, TimestampMixin):
    __tablename__ = "dataset_examples"

    project_id: Mapped[uuid.UUID] = mapped_column()
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    # Stable identity across versions. Comparison matches on this rather than on
    # position, because datasets gain and lose rows and ordinal matching would
    # silently compare unrelated examples.
    external_id: Mapped[str] = mapped_column(String(200))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expected: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    example_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    source_trace_id: Mapped[str | None] = mapped_column(String(64), default=None)
    source_span_id: Mapped[str | None] = mapped_column(String(64), default=None)

    version: Mapped[DatasetVersion] = relationship(back_populates="examples")

    __table_args__ = (
        UniqueConstraint("dataset_version_id", "ordinal", name="uq_dataset_examples_ordinal"),
        UniqueConstraint("dataset_version_id", "external_id", name="uq_dataset_examples_external"),
        Index("ix_dataset_examples_version", "project_id", "dataset_version_id", "ordinal"),
    )


# -------------------------------------------------------------------- evaluators


class Evaluator(IdentifiedBase, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "evaluators"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))
    evaluator_type: Mapped[str] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(Text, default=None)

    versions: Mapped[list[EvaluatorVersion]] = relationship(back_populates="evaluator")

    __table_args__ = (
        Index(
            "uq_evaluators_project_slug_active",
            "project_id",
            "slug",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
        CheckConstraint(f"evaluator_type IN {EVALUATOR_TYPES}", name="evaluator_type_valid"),
    )


class EvaluatorVersion(IdentifiedBase, TimestampMixin):
    """Immutable. The config hash covers everything that could change a score."""

    __tablename__ = "evaluator_versions"

    project_id: Mapped[uuid.UUID] = mapped_column()
    evaluator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluators.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    judge_model: Mapped[str | None] = mapped_column(String(200), default=None)
    judge_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    code_ref: Mapped[str | None] = mapped_column(
        String(500),
        default=None,
        comment="git path and sha for custom_python; the server never executes it",
    )
    output_kind: Mapped[str] = mapped_column(String(20), default="score")

    evaluator: Mapped[Evaluator] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("evaluator_id", "version", name="uq_evaluator_versions_number"),
        # Prevents minting a "new version" that is byte-identical to an existing one,
        # which would make two runs look incomparable when they are not.
        UniqueConstraint("evaluator_id", "config_hash", name="uq_evaluator_versions_config"),
        CheckConstraint(f"output_kind IN {OUTPUT_KINDS}", name="output_kind_valid"),
    )


class EvaluatorCalibration(IdentifiedBase, TimestampMixin):
    """Evidence that a judge agrees with a human, keyed to an evaluator *version*.

    Append-only by convention: a calibration is a measurement taken at a point in time,
    and overwriting it would destroy the history that answers "when did this judge get
    worse, and what changed?". The newest row for a version wins.

    Keyed to the version, not the evaluator. A judge whose rubric, model, or parameters
    changed has a different config hash and therefore no calibration at all — which is
    the intended behaviour, because the old evidence describes a different ruler.
    """

    __tablename__ = "evaluator_calibrations"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    evaluator_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluator_versions.id", ondelete="CASCADE")
    )
    calibration_dataset_version_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    #: Hash of the labelled file, so a changed number can be attributed to a changed
    #: judge or a changed labelled set rather than being unattributable.
    labels_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    n_examples: Mapped[int] = mapped_column(Integer, default=0)
    n_errored: Mapped[int] = mapped_column(Integer, default=0)
    agreement: Mapped[float | None] = mapped_column(Float, default=None)
    #: Nullable on purpose. κ is undefined when both raters used a single label, and
    #: storing 0.0 there would read as "no better than chance" for a judge that was never
    #: wrong; storing 1.0 would certify one that always answers the same thing.
    cohens_kappa: Mapped[float | None] = mapped_column(Float, default=None)
    kappa_kind: Mapped[str] = mapped_column(String(20), default="unweighted")
    kappa_undefined_reason: Mapped[str | None] = mapped_column(Text, default=None)
    #: Also nullable, and for the same class of reason: a rate over an empty denominator
    #: is unmeasured, not zero, and a gate must not read the two the same way.
    false_pass_rate: Mapped[float | None] = mapped_column(Float, default=None)
    false_fail_rate: Mapped[float | None] = mapped_column(Float, default=None)
    human_kappa: Mapped[float | None] = mapped_column(Float, default=None)
    n_ceiling_examples: Mapped[int] = mapped_column(Integer, default=0)

    confusion_matrix: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    per_class: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    position_bias: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    mean_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    p50_latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    p95_latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    judge_model: Mapped[str | None] = mapped_column(String(200), default=None)

    requirement: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    satisfied: Mapped[bool] = mapped_column(Boolean, default=False)
    failures: Mapped[list[str]] = mapped_column(JSONB, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)
    notes: Mapped[list[str]] = mapped_column(JSONB, default=list)

    __table_args__ = (
        Index(
            "ix_evaluator_calibrations_version",
            "project_id",
            "evaluator_version_id",
            "created_at",
        ),
        CheckConstraint(
            "agreement IS NULL OR (agreement >= 0 AND agreement <= 1)",
            name="agreement_ratio",
        ),
        CheckConstraint(
            "cohens_kappa IS NULL OR (cohens_kappa >= -1 AND cohens_kappa <= 1)",
            name="kappa_range",
        ),
        CheckConstraint("n_examples >= 0", name="n_examples_non_negative"),
    )


# ------------------------------------------------------------------- experiments


class Experiment(IdentifiedBase, TimestampMixin):
    """The *definition*: what is being tested, and against what.

    Every field that affects a result is recorded here. That is the whole
    reproducibility contract — a run you cannot reconstruct is an anecdote.
    """

    __tablename__ = "experiments"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    environment_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    name: Mapped[str] = mapped_column(String(200))
    suite_name: Mapped[str] = mapped_column(String(200))

    dataset_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="SET NULL"), default=None
    )
    dataset_content_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), default=None)

    task_ref: Mapped[str | None] = mapped_column(String(300), default=None)
    task_version: Mapped[str | None] = mapped_column(String(100), default=None)
    evaluator_version_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(String(36)), default=list)
    policy_version_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(String(36)), default=list)
    gate_set_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    git_commit: Mapped[str | None] = mapped_column(String(64), default=None)
    git_branch: Mapped[str | None] = mapped_column(String(200), default=None)
    git_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    dependency_lock_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Promotion is curation, not a data change, so these stay mutable while
    # everything else about an experiment does not.
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    baseline_label: Mapped[str | None] = mapped_column(String(100), default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    runs: Mapped[list[ExperimentRun]] = relationship(back_populates="experiment")

    __table_args__ = (
        Index("ix_experiments_project_suite", "project_id", "suite_name", "created_at"),
        Index("ix_experiments_project_branch", "project_id", "git_branch", "created_at"),
        Index(
            "ix_experiments_baselines",
            "project_id",
            "suite_name",
            postgresql_where="is_baseline",
        ),
    )


class ExperimentRun(IdentifiedBase, TimestampMixin):
    """The *execution*. Splitting this from the definition is what makes resume,
    retry-failed, and partial completion expressible without mutating history."""

    __tablename__ = "experiment_runs"

    project_id: Mapped[uuid.UUID] = mapped_column()
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE")
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    trigger: Mapped[str] = mapped_column(String(20), default="cli")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    total_examples: Mapped[int] = mapped_column(Integer, default=0)
    completed_examples: Mapped[int] = mapped_column(Integer, default=0)
    failed_examples: Mapped[int] = mapped_column(Integer, default=0)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    runner: Mapped[str | None] = mapped_column(String(100), default=None)
    runner_version: Mapped[str | None] = mapped_column(String(50), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    experiment: Mapped[Experiment] = relationship(back_populates="runs")

    __table_args__ = (
        UniqueConstraint("experiment_id", "attempt", name="uq_experiment_runs_attempt"),
        CheckConstraint(f"status IN {RUN_STATUSES}", name="status_valid"),
        CheckConstraint(f"trigger IN {RUN_TRIGGERS}", name="trigger_valid"),
        Index("ix_experiment_runs_project_status", "project_id", "status", "created_at"),
    )


class ExperimentResult(IdentifiedBase, TimestampMixin):
    """One row per example per run. Append-only."""

    __tablename__ = "experiment_results"

    project_id: Mapped[uuid.UUID] = mapped_column()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiment_runs.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="ok")
    output: Mapped[Any | None] = mapped_column(JSONB, default=None)
    output_ref: Mapped[uuid.UUID | None] = mapped_column(default=None)
    trace_id: Mapped[str | None] = mapped_column(String(64), default=None)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        UniqueConstraint("run_id", "external_id", name="uq_experiment_results_example"),
        CheckConstraint(f"status IN {RESULT_STATUSES}", name="status_valid"),
        Index("ix_experiment_results_run_status", "project_id", "run_id", "status"),
    )


class EvaluationResult(IdentifiedBase, TimestampMixin):
    """One row per (result x evaluator version). Also used for online evaluation,
    where `run_id` is null and the trace is set instead."""

    __tablename__ = "evaluation_results"

    project_id: Mapped[uuid.UUID] = mapped_column()
    experiment_result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiment_results.id", ondelete="CASCADE"), default=None
    )
    trace_id: Mapped[str | None] = mapped_column(String(64), default=None)
    span_id: Mapped[str | None] = mapped_column(String(64), default=None)
    evaluator_version_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    metric_key: Mapped[str] = mapped_column(String(200))
    score: Mapped[float | None] = mapped_column(Float, default=None)
    passed: Mapped[bool | None] = mapped_column(Boolean, default=None)
    label: Mapped[str | None] = mapped_column(String(200), default=None)
    value_json: Mapped[Any | None] = mapped_column(JSONB, default=None)
    slice: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    reasoning: Mapped[str | None] = mapped_column(Text, default=None)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)
    # An errored evaluation is not a score of zero. Aggregation excludes these from
    # the mean and counts them separately, and a gate on a metric with too many
    # errors reports ERROR rather than passing.
    error: Mapped[str | None] = mapped_column(Text, default=None)
    cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    evaluator_trace_id: Mapped[str | None] = mapped_column(String(64), default=None)

    __table_args__ = (
        CheckConstraint(
            "(experiment_result_id IS NOT NULL) OR (trace_id IS NOT NULL)",
            name="attached_to_something",
        ),
        CheckConstraint(
            "error IS NULL OR score IS NULL",
            name="error_excludes_score",
        ),
        Index("ix_evaluation_results_result", "project_id", "experiment_result_id"),
        Index("ix_evaluation_results_metric", "project_id", "metric_key", "created_at"),
    )


class AggregateMetric(IdentifiedBase, TimestampMixin):
    """Precomputed per-run rollups, so comparison is a cheap join rather than a
    re-aggregation over every evaluation row."""

    __tablename__ = "aggregate_metrics"

    project_id: Mapped[uuid.UUID] = mapped_column()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiment_runs.id", ondelete="CASCADE"))
    metric_key: Mapped[str] = mapped_column(String(200))
    slice_key: Mapped[str] = mapped_column(
        String(300),
        default="",
        comment="Canonical slice rendering, empty when unsliced; keeps the unique index simple",
    )
    slice: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    value: Mapped[float] = mapped_column(Float)
    count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    stddev: Mapped[float | None] = mapped_column(Float, default=None)
    ci_low: Mapped[float | None] = mapped_column(Float, default=None)
    ci_high: Mapped[float | None] = mapped_column(Float, default=None)
    unit: Mapped[str | None] = mapped_column(String(20), default=None)

    __table_args__ = (
        UniqueConstraint("run_id", "metric_key", "slice_key", name="uq_aggregate_metrics_key"),
        Index("ix_aggregate_metrics_run", "project_id", "run_id"),
    )


# ------------------------------------------------------------- gates and policies


class QualityGateSet(IdentifiedBase, TimestampMixin):
    __tablename__ = "quality_gate_sets"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_yaml: Mapped[str | None] = mapped_column(Text, default=None)
    require_dataset_match: Mapped[bool] = mapped_column(Boolean, default=True)
    require_calibration: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The thresholds, when the suite gave any. Stored separately from the boolean so
    #: "enforced with the recommended defaults" and "enforced with these numbers" are
    #: distinguishable — otherwise a tightened threshold would be invisible in the mirror
    #: of what the repository declared.
    calibration_requirement: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    rules: Mapped[list[QualityGateRule]] = relationship(back_populates="gate_set")

    __table_args__ = (
        UniqueConstraint("project_id", "name", "version", name="uq_quality_gate_sets_version"),
    )


class QualityGateRule(IdentifiedBase, TimestampMixin):
    __tablename__ = "quality_gate_rules"

    project_id: Mapped[uuid.UUID] = mapped_column()
    gate_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quality_gate_sets.id", ondelete="CASCADE")
    )
    metric_key: Mapped[str] = mapped_column(String(200))
    minimum: Mapped[float | None] = mapped_column(Float, default=None)
    maximum: Mapped[float | None] = mapped_column(Float, default=None)
    max_absolute_regression: Mapped[float | None] = mapped_column(Float, default=None)
    max_relative_regression: Mapped[float | None] = mapped_column(Float, default=None)
    severity: Mapped[str] = mapped_column(String(10), default="block")
    # What makes protected-class gating possible: a rule can target the rare class
    # directly instead of the macro average that hides it.
    slice: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    slice_key: Mapped[str] = mapped_column(String(300), default="")
    require_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    max_error_rate: Mapped[float] = mapped_column(Float, default=0.05)

    gate_set: Mapped[QualityGateSet] = relationship(back_populates="rules")

    __table_args__ = (
        UniqueConstraint(
            "gate_set_id", "metric_key", "slice_key", name="uq_quality_gate_rules_key"
        ),
        CheckConstraint(f"severity IN {SEVERITIES}", name="severity_valid"),
    )


class TrajectoryPolicy(IdentifiedBase, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "trajectory_policies"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text, default=None)

    versions: Mapped[list[TrajectoryPolicyVersion]] = relationship(back_populates="policy")

    __table_args__ = (
        Index(
            "uq_trajectory_policies_project_slug_active",
            "project_id",
            "slug",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )


class TrajectoryPolicyVersion(IdentifiedBase, TimestampMixin):
    __tablename__ = "trajectory_policy_versions"

    project_id: Mapped[uuid.UUID] = mapped_column()
    policy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trajectory_policies.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    # The original text is always retained, not just the parsed form, so error
    # messages can point at a line number the author will recognise.
    source_yaml: Mapped[str] = mapped_column(Text)
    parsed: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary(32))

    policy: Mapped[TrajectoryPolicy] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_trajectory_policy_versions_number"),
    )
