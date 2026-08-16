"""Online evaluation, review queues, and annotations.

The loop this closes: a production trace arrives, gets checked, and a failure becomes a
review item that a human can turn into a dataset example. That last step is what stops
evaluation being a dashboard — a failure nobody converts into a test is a failure that
recurs.

Three schema decisions that are not obvious:

- **No foreign key onto `traces`.** That table is RANGE-partitioned, and a foreign key
  into a partitioned table forces the parent to be indexed in a way that defeats partition
  pruning and blocks `DETACH`. Retention drops whole partitions, so the FK would have to be
  dropped anyway. Integrity is by natural key `(project_id, trace_id)`.

- **`(project_id, trace_id, rule_id)` is unique.** A worker that re-processes a batch after
  a crash must not double-count a trace, and an online metric that drifts upward every time
  a queue is replayed is worse than no metric.

- **Review claims carry a lease.** A reviewer who claims an item and closes their laptop
  must not hold it forever. Without an expiry, a queue silently drains into a pool of
  items that are assigned to nobody who is coming back.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from proofstep_api.db.base import IdentifiedBase, SoftDeleteMixin, TimestampMixin, UpdatedAtMixin

RULE_KINDS = ("trajectory", "llm_judge", "deterministic")
#: `inconclusive` is a first-class outcome, not a flavour of failure. A trace that lost
#: spans cannot answer a question about what did not happen, and calling that a violation
#: would fill a review queue with innocent traces until people stopped reading it.
EVALUATION_VERDICTS = ("pass", "fail", "inconclusive", "error", "skipped")
#: `budget` is distinct from `capped` on purpose. `capped` means this batch's escalation allowance
#: ran out and the next batch picks the trace up; `budget` means the project's monthly ceiling is
#: reached and nothing paid runs until the month turns or the limit rises. Collapsing them would
#: make "why was this not judged?" unanswerable at the moment someone is asking.
DECISION_REASONS = (
    "deterministic",
    "sampled",
    "escalated",
    "forced",
    "not_sampled",
    "capped",
    "budget",
)
TARGET_TYPES = ("trace", "span", "experiment_result", "dataset_example")
ASSIGNMENT_STATUSES = ("pending", "in_review", "done", "skipped")
PREFERENCES = ("a", "b", "tie")


class OnlineEvalRule(IdentifiedBase, TimestampMixin, UpdatedAtMixin, SoftDeleteMixin):
    """One thing to check on incoming traces, and how often.

    A rule is either free (a trajectory policy, a deterministic check) or paid (a judge),
    and the distinction drives everything else: free rules run on every trace because
    sampling them would save nothing and lose coverage of the safety properties most worth
    having everywhere.
    """

    __tablename__ = "online_eval_rules"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    environment_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    policy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trajectory_policy_versions.id", ondelete="RESTRICT"), default=None
    )
    evaluator_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evaluator_versions.id", ondelete="RESTRICT"), default=None
    )

    #: Ignored for `deterministic` and `trajectory` kinds, which always run.
    sample_rate: Mapped[float] = mapped_column(Float, default=0.01)
    #: Rules sharing a group sample the *same* traces. Left null, each rule samples
    #: independently — otherwise every rule at 1% would select the identical 1% and the
    #: other 99% would be invisible to every judge in the project.
    sample_group: Mapped[str | None] = mapped_column(String(100), default=None)
    escalate_on_failure: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Cap on escalations per worker batch. An incident produces an error spike, which
    #: without this produces a judge-call spike and a surprise bill.
    max_escalations_per_batch: Mapped[int] = mapped_column(Integer, default=50)

    #: Only evaluate traces with this name. Null means every trace.
    trace_name: Mapped[str | None] = mapped_column(String(200), default=None)

    #: Where a failure goes for human attention.
    review_queue_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("review_queues.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (
        Index(
            "uq_online_eval_rules_project_slug_active",
            "project_id",
            "slug",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
        Index(
            "ix_online_eval_rules_enabled",
            "project_id",
            "enabled",
            postgresql_where="deleted_at IS NULL",
        ),
        CheckConstraint(f"kind IN {RULE_KINDS}", name="rule_kind_valid"),
        CheckConstraint("sample_rate >= 0 AND sample_rate <= 1", name="online_sample_rate_ratio"),
        CheckConstraint("max_escalations_per_batch >= 0", name="max_escalations_non_negative"),
        # A rule has to have something to run. A rule that names neither a policy nor an
        # evaluator would silently evaluate nothing while appearing configured.
        CheckConstraint(
            "(kind = 'trajectory' AND policy_version_id IS NOT NULL) OR "
            "(kind = 'llm_judge' AND evaluator_version_id IS NOT NULL) OR "
            "(kind = 'deterministic' AND evaluator_version_id IS NOT NULL)",
            name="rule_targets_something",
        ),
    )


class OnlineEvaluation(IdentifiedBase, TimestampMixin):
    """The result of applying one rule to one trace.

    Rows exist for skipped traces too, carrying the reason. That is deliberate: "this trace
    has no score" is ambiguous between "not sampled", "the escalation budget ran out", and
    "the rule errored", and those have entirely different responses. Recording the decision
    makes coverage auditable rather than inferred.
    """

    __tablename__ = "online_evaluations"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    #: Natural key, not a foreign key — `traces` is partitioned. See the module docstring.
    trace_id: Mapped[str] = mapped_column(String(64))
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("online_eval_rules.id", ondelete="CASCADE")
    )

    verdict: Mapped[str] = mapped_column(String(16))
    decision_reason: Mapped[str] = mapped_column(String(20))
    score: Mapped[float | None] = mapped_column(Float, default=None)
    #: Trajectory failures, or a judge's reasoning. Kept as JSONB because the shape differs
    #: per rule kind and normalising it would mean a table per kind.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    #: Populated when the evaluation itself broke. An errored evaluation is not a failing
    #: trace, and scoring it as one turns a provider outage into a quality regression.
    error: Mapped[str | None] = mapped_column(Text, default=None)

    cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    trace_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        comment="copied from the trace so rollups need no join into a partitioned table",
    )

    __table_args__ = (
        # Idempotency. A worker replaying a batch after a crash must not double-count.
        UniqueConstraint(
            "project_id", "trace_id", "rule_id", name="uq_online_evaluations_trace_rule"
        ),
        Index("ix_online_evaluations_rule_time", "project_id", "rule_id", "created_at"),
        Index(
            "ix_online_evaluations_failures",
            "project_id",
            "rule_id",
            "created_at",
            postgresql_where="verdict = 'fail'",
        ),
        CheckConstraint(f"verdict IN {EVALUATION_VERDICTS}", name="online_verdict_valid"),
        CheckConstraint(f"decision_reason IN {DECISION_REASONS}", name="decision_reason_valid"),
    )


class ReviewQueue(IdentifiedBase, TimestampMixin, SoftDeleteMixin):
    """A named backlog of things for humans to look at."""

    __tablename__ = "review_queues"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: What routes into this queue. Advisory metadata for the UI; routing is done by the
    #: rule that points at the queue, so a filter here cannot silently drop items.
    filter: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    #: How long a claim is held before it returns to the pool.
    lease_seconds: Mapped[int] = mapped_column(Integer, default=1800)

    __table_args__ = (
        Index(
            "uq_review_queues_project_slug_active",
            "project_id",
            "slug",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
        CheckConstraint("lease_seconds > 0", name="lease_seconds_positive"),
    )


class ReviewAssignment(IdentifiedBase, TimestampMixin, UpdatedAtMixin):
    """One item in a queue, and who is looking at it.

    Claimed with `FOR UPDATE SKIP LOCKED`, so two reviewers hitting "next" at the same
    moment get different items rather than one of them waiting on a row lock or both
    getting the same one.
    """

    __tablename__ = "review_assignments"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    queue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("review_queues.id", ondelete="CASCADE"))
    target_type: Mapped[str] = mapped_column(String(20))
    #: Polymorphic, so no foreign key. The alternative — four nullable FK columns — makes
    #: every query in this table uglier for integrity a nightly check can provide.
    target_id: Mapped[str] = mapped_column(String(64))
    #: Set when the item came from an online evaluation, so a reviewer can see what failed
    #: rather than being handed a trace with no context.
    online_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("online_evaluations.id", ondelete="SET NULL"), default=None
    )

    status: Mapped[str] = mapped_column(String(16), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: When the claim lapses. A reviewer who walks away must not hold an item forever.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # One item per target per queue. Without this, a rule that fires twice on the same
        # trace would queue it twice and two reviewers would do the same work.
        UniqueConstraint(
            "queue_id", "target_type", "target_id", name="uq_review_assignments_target"
        ),
        # The claim query's index: highest priority, oldest first, pending only.
        Index(
            "ix_review_assignments_claimable",
            "project_id",
            "queue_id",
            "priority",
            "created_at",
            postgresql_where="status = 'pending'",
        ),
        Index(
            "ix_review_assignments_leases",
            "lease_expires_at",
            postgresql_where="status = 'in_review'",
        ),
        CheckConstraint(f"status IN {ASSIGNMENT_STATUSES}", name="assignment_status_valid"),
        CheckConstraint(f"target_type IN {TARGET_TYPES}", name="assignment_target_type_valid"),
    )


class Annotation(IdentifiedBase, TimestampMixin, UpdatedAtMixin):
    """A human judgement about something.

    This is the ground truth everything else is measured against — judge calibration, and
    the golden datasets promoted out of production. It is never written by a model: an
    LLM-labelled "ground truth" makes calibration circular, and the whole point of this
    table is to be the thing a model can be wrong about.
    """

    __tablename__ = "annotations"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    target_type: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(64))
    annotator_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    label: Mapped[str | None] = mapped_column(String(100), default=None)
    rating: Mapped[float | None] = mapped_column(Float, default=None)
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    #: What the output *should* have been. This is what makes an annotation promotable into
    #: a dataset example rather than only a complaint about one.
    correction: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    #: Pairwise comparison, when the annotation is a preference between two targets.
    preference_target_id: Mapped[str | None] = mapped_column(String(64), default=None)
    preference_winner: Mapped[str | None] = mapped_column(String(4), default=None)

    __table_args__ = (
        Index("ix_annotations_target", "project_id", "target_type", "target_id"),
        Index("ix_annotations_annotator", "project_id", "annotator_id", "created_at"),
        CheckConstraint(f"target_type IN {TARGET_TYPES}", name="annotation_target_type_valid"),
        CheckConstraint(
            f"preference_winner IS NULL OR preference_winner IN {PREFERENCES}",
            name="preference_winner_valid",
        ),
        # An annotation with no content is a row that says nothing. Requiring one field
        # keeps "I looked at this and had no opinion" out of the ground-truth table, where
        # it would count as a label.
        CheckConstraint(
            "label IS NOT NULL OR rating IS NOT NULL OR comment IS NOT NULL "
            "OR correction IS NOT NULL OR preference_winner IS NOT NULL",
            name="annotation_says_something",
        ),
        CheckConstraint(
            "preference_winner IS NULL OR preference_target_id IS NOT NULL",
            name="preference_needs_a_counterpart",
        ),
    )
