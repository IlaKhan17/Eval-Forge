"""Online evaluation, review queues, annotations, and promotion.

The routes that close the loop: production traces get checked, failures reach a human, and
the human turns one into a dataset example.

One access-control note worth stating, because it is easy to get backwards. Claiming and
annotating are **write** operations even though they feel like reading: they mutate queue
state and they create ground truth. A read-only credential must not be able to claim an item
(it would take work away from reviewers who can finish it) or write an annotation (it would
inject unattributable labels into the table that calibration is measured against).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from evalforge_api.api.dependencies import SessionDep, get_principal
from evalforge_api.db.models.online import (
    OnlineEvalRule,
    OnlineEvaluation,
    ReviewAssignment,
    ReviewQueue,
)
from evalforge_api.errors import ForbiddenError, NotFoundError, UnprocessableError
from evalforge_api.security.permissions import Permission, Principal
from evalforge_api.services.online_eval import OnlineEvalService, coverage, unprocessed_count
from evalforge_api.services.review import ReviewService
from evalforge_api.worker import jobs

router = APIRouter(prefix="/v1", tags=["online"])


def _guard(permission: Permission) -> Any:
    async def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if principal.project_id is None:
            raise ForbiddenError("This endpoint requires a project-scoped credential.")
        if not principal.can(permission):
            raise ForbiddenError(f"This action requires the {permission.value!r} permission.")
        return principal

    return Depends(dependency)


# Distinct scopes rather than one blanket "write", mapped onto the existing role tiers.
# The one that matters: a *reviewer* can claim and annotate without being able to change
# rules or datasets. Requiring a configuration scope to do review work would push everyone
# to hand reviewers a developer credential, which is how least privilege dies in practice.
Reader = Annotated[Principal, _guard(Permission.PROJECT_READ)]
Reviewer = Annotated[Principal, _guard(Permission.ANNOTATION_WRITE)]
Configurer = Annotated[Principal, _guard(Permission.POLICY_WRITE)]
Runner = Annotated[Principal, _guard(Permission.EXPERIMENT_RUN)]
DatasetWriter = Annotated[Principal, _guard(Permission.DATASET_WRITE)]


# --------------------------------------------------------------------------- schemas


class RuleIn(BaseModel):
    name: str = Field(max_length=200)
    slug: str = Field(max_length=100, pattern=r"^[a-z][a-z0-9-]*$")
    kind: str
    enabled: bool = True
    policy_version_id: uuid.UUID | None = None
    evaluator_version_id: uuid.UUID | None = None
    sample_rate: float = Field(default=0.01, ge=0, le=1)
    sample_group: str | None = Field(default=None, max_length=100)
    escalate_on_failure: bool = True
    max_escalations_per_batch: int = Field(default=50, ge=0)
    trace_name: str | None = Field(default=None, max_length=200)
    review_queue_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _targets_something(self) -> RuleIn:
        if self.kind == "trajectory" and self.policy_version_id is None:
            msg = "a trajectory rule needs a policy_version_id"
            raise ValueError(msg)
        if self.kind in ("llm_judge", "deterministic") and self.evaluator_version_id is None:
            msg = f"a {self.kind} rule needs an evaluator_version_id"
            raise ValueError(msg)
        return self


class RuleOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    kind: str
    enabled: bool
    sample_rate: float
    sample_group: str | None
    escalate_on_failure: bool
    max_escalations_per_batch: int
    trace_name: str | None
    policy_version_id: uuid.UUID | None
    evaluator_version_id: uuid.UUID | None
    review_queue_id: uuid.UUID | None


class QueueIn(BaseModel):
    name: str = Field(max_length=200)
    slug: str = Field(max_length=100, pattern=r"^[a-z][a-z0-9-]*$")
    description: str | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    lease_seconds: int = Field(default=1800, gt=0, le=86_400)


class QueueOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    lease_seconds: int
    depth: dict[str, int] = Field(default_factory=dict)


class AssignmentOut(BaseModel):
    id: uuid.UUID
    queue_id: uuid.UUID
    target_type: str
    target_id: str
    status: str
    priority: int
    reason: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    #: What failed, when the item came from an online evaluation. Without it a reviewer is
    #: handed a trace and no reason to look at it.
    evaluation: dict[str, Any] | None = None


class AnnotationIn(BaseModel):
    target_type: str = "trace"
    target_id: str = Field(max_length=64)
    label: str | None = Field(default=None, max_length=100)
    rating: float | None = None
    comment: str | None = None
    #: What the output should have been. This is what makes the annotation promotable.
    correction: dict[str, Any] | None = None
    preference_target_id: str | None = Field(default=None, max_length=64)
    preference_winner: str | None = None


class AnnotationOut(BaseModel):
    id: uuid.UUID
    target_type: str
    target_id: str
    annotator_id: uuid.UUID | None
    label: str | None
    rating: float | None
    comment: str | None
    correction: dict[str, Any] | None
    created_at: datetime


class PromoteIn(BaseModel):
    trace_id: str = Field(max_length=64)
    dataset_slug: str = Field(max_length=100)
    expected: dict[str, Any] | None = None
    annotation_id: uuid.UUID | None = None
    input_from_span: str | None = Field(default=None, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromoteOut(BaseModel):
    dataset_version_id: uuid.UUID
    example_id: str
    created_draft: bool
    already_present: bool


# ----------------------------------------------------------------------------- rules


@router.post("/online-rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleIn, session: SessionDep, principal: Configurer) -> RuleOut:
    existing = (
        await session.execute(
            select(OnlineEvalRule).where(
                OnlineEvalRule.project_id == principal.project_id,
                OnlineEvalRule.slug == body.slug,
                OnlineEvalRule.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Upsert by slug. A CI job that registers its rules on every run must not create a
        # duplicate, and must not fail because it already succeeded once.
        for field_name, value in body.model_dump(exclude={"slug"}).items():
            setattr(existing, field_name, value)
        await session.flush()
        return _rule_out(existing)

    row = OnlineEvalRule(project_id=principal.project_id, **body.model_dump())
    session.add(row)
    await session.flush()
    return _rule_out(row)


@router.get("/online-rules", response_model=list[RuleOut])
async def list_rules(session: SessionDep, principal: Reader) -> list[RuleOut]:
    rows = (
        (
            await session.execute(
                select(OnlineEvalRule)
                .where(
                    OnlineEvalRule.project_id == principal.project_id,
                    OnlineEvalRule.deleted_at.is_(None),
                )
                .order_by(OnlineEvalRule.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_rule_out(row) for row in rows]


@router.get("/online-rules/{rule_id}/coverage", response_model=dict[str, Any])
async def rule_coverage(
    rule_id: uuid.UUID,
    session: SessionDep,
    principal: Reader,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> dict[str, Any]:
    """How many traces each decision accounts for, and how many are still unprocessed.

    The number that makes online evaluation auditable. "97% of traces were not sampled" and
    "97% of traces were never processed" produce the same pass rate and mean completely
    different things — only one of them says the worker is behind.
    """
    rule = await session.get(OnlineEvalRule, rule_id)
    if rule is None or rule.project_id != principal.project_id:
        raise NotFoundError("No such online rule.")

    since = datetime.now(UTC) - timedelta(hours=hours)
    by_reason = await coverage(
        session, project_id=principal.project_id, rule_id=rule_id, since=since
    )
    backlog = await unprocessed_count(
        session, project_id=principal.project_id, rule_id=rule_id, since=since
    )
    return {
        "rule": rule.slug,
        "window_hours": hours,
        "by_decision": by_reason,
        "unprocessed": backlog,
    }


@router.post("/online-rules/run", response_model=dict[str, Any])
async def run_now(
    session: SessionDep,
    principal: Runner,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 200,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> dict[str, Any]:
    """Process a batch immediately, rather than waiting for the worker.

    Exists for two real needs: draining a backlog after an outage without waiting for the
    cron cadence, and letting a test drive the whole path without a Redis. Bounded by `limit`
    so it cannot be used to start an unbounded amount of paid work from one request.
    """
    service = OnlineEvalService(session, project_id=principal.project)
    outcome = await service.run_batch(since=datetime.now(UTC) - timedelta(hours=hours), limit=limit)
    return {
        "traces_considered": outcome.traces_considered,
        "evaluations_written": outcome.evaluations_written,
        "failures": outcome.failures,
        "errors": outcome.errors,
        "skipped": outcome.skipped,
        "queued_for_review": outcome.queued_for_review,
        "cost": str(outcome.cost),
        "by_decision": outcome.reasons,
    }


# ---------------------------------------------------------------------------- queues


@router.post("/review-queues", response_model=QueueOut, status_code=status.HTTP_201_CREATED)
async def create_queue(body: QueueIn, session: SessionDep, principal: Configurer) -> QueueOut:
    existing = (
        await session.execute(
            select(ReviewQueue).where(
                ReviewQueue.project_id == principal.project_id,
                ReviewQueue.slug == body.slug,
                ReviewQueue.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return QueueOut(
            id=existing.id,
            name=existing.name,
            slug=existing.slug,
            lease_seconds=existing.lease_seconds,
        )

    row = ReviewQueue(project_id=principal.project_id, **body.model_dump())
    session.add(row)
    await session.flush()
    return QueueOut(id=row.id, name=row.name, slug=row.slug, lease_seconds=row.lease_seconds)


@router.get("/review-queues", response_model=list[QueueOut])
async def list_queues(session: SessionDep, principal: Reader) -> list[QueueOut]:
    rows = (
        (
            await session.execute(
                select(ReviewQueue)
                .where(
                    ReviewQueue.project_id == principal.project_id,
                    ReviewQueue.deleted_at.is_(None),
                )
                .order_by(ReviewQueue.created_at)
            )
        )
        .scalars()
        .all()
    )
    service = ReviewService(session, project_id=principal.project)
    return [
        QueueOut(
            id=row.id,
            name=row.name,
            slug=row.slug,
            lease_seconds=row.lease_seconds,
            depth=await service.queue_depth(row.id),
        )
        for row in rows
    ]


@router.post("/review-queues/{queue_id}/claim", response_model=AssignmentOut | None)
async def claim(
    queue_id: uuid.UUID, session: SessionDep, principal: Reviewer
) -> AssignmentOut | None:
    """Take the next item. A write, despite reading like a read — it mutates queue state."""
    service = ReviewService(session, project_id=principal.project)
    claimed = await service.claim_next(queue_id=queue_id, reviewer_id=_reviewer(principal))
    if claimed is None:
        return None
    return _assignment_out(claimed.assignment, claimed.evaluation)


@router.post("/review-assignments/{assignment_id}/complete", response_model=AssignmentOut)
async def complete(
    assignment_id: uuid.UUID,
    session: SessionDep,
    principal: Reviewer,
    outcome: Annotated[str, Query(pattern="^(done|skipped)$")] = "done",
) -> AssignmentOut:
    service = ReviewService(session, project_id=principal.project)
    assignment = await service.complete(assignment_id, status=outcome)
    return _assignment_out(assignment, None)


@router.get("/review-queues/{queue_id}/items", response_model=list[AssignmentOut])
async def list_items(
    queue_id: uuid.UUID,
    session: SessionDep,
    principal: Reader,
    item_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AssignmentOut]:
    service = ReviewService(session, project_id=principal.project)
    await service.get_queue(queue_id)

    statement = (
        select(ReviewAssignment)
        .where(
            ReviewAssignment.project_id == principal.project_id,
            ReviewAssignment.queue_id == queue_id,
        )
        .order_by(ReviewAssignment.priority.desc(), ReviewAssignment.created_at)
        .limit(limit)
    )
    if item_status:
        statement = statement.where(ReviewAssignment.status == item_status)

    rows = (await session.execute(statement)).scalars().all()
    return [_assignment_out(row, None) for row in rows]


@router.get("/review-queues/health", response_model=dict[str, Any])
async def health(session: SessionDep, principal: Reader) -> dict[str, Any]:
    """Depth plus the age of the oldest pending item, per queue.

    Age is the number that matters. A queue of 500 items raised this morning is a busy day; a
    queue of 5 where the oldest is three weeks old means nobody is reading it, and a review
    queue nobody reads has stopped being a control.
    """
    return await jobs.queue_health(session, project_id=principal.project)


# ----------------------------------------------------------------------- annotations


@router.post("/annotations", response_model=AnnotationOut, status_code=status.HTTP_201_CREATED)
async def annotate(body: AnnotationIn, session: SessionDep, principal: Reviewer) -> AnnotationOut:
    """Record a human judgement.

    A write scope, and attributed to whoever holds the credential. An annotation is ground
    truth; an unattributable one cannot be questioned later, and questioning a label is
    exactly what adjudicating a judge-human disagreement requires.
    """
    service = ReviewService(session, project_id=principal.project)
    row = await service.annotate(
        target_type=body.target_type,
        target_id=body.target_id,
        annotator_id=_reviewer(principal),
        label=body.label,
        rating=body.rating,
        comment=body.comment,
        correction=body.correction,
        preference_target_id=body.preference_target_id,
        preference_winner=body.preference_winner,
    )
    return _annotation_out(row)


@router.get("/annotations", response_model=list[AnnotationOut])
async def list_annotations(
    session: SessionDep,
    principal: Reader,
    target_type: str = "trace",
    target_id: str = "",
) -> list[AnnotationOut]:
    if not target_id:
        raise UnprocessableError("target_id is required.")
    service = ReviewService(session, project_id=principal.project)
    rows = await service.annotations_for(target_type=target_type, target_id=target_id)
    return [_annotation_out(row) for row in rows]


# ------------------------------------------------------------------------ promotion


@router.post("/datasets/promote-from-trace", response_model=PromoteOut)
async def promote(body: PromoteIn, session: SessionDep, principal: DatasetWriter) -> PromoteOut:
    """Turn a production trace into a dataset example.

    Always into a **draft** version. A locked version's content hash is what lets an
    experiment prove it saw identical data, so appending to one would silently invalidate
    every historical comparison against it.

    The expected result must come from a human — directly, or from an annotation's
    correction. Promoting with the model's own output as the expected answer would enshrine
    the defect as the specification.
    """
    service = ReviewService(session, project_id=principal.project)
    outcome = await service.promote_trace(
        trace_id=body.trace_id,
        dataset_slug=body.dataset_slug,
        expected=body.expected,
        annotation_id=body.annotation_id,
        input_from_span=body.input_from_span,
        metadata=body.metadata,
    )
    return PromoteOut(
        dataset_version_id=outcome.dataset_version_id,
        example_id=outcome.example_id,
        created_draft=outcome.created_draft,
        already_present=outcome.already_present,
    )


# -------------------------------------------------------------------------- mapping


def _reviewer(principal: Principal) -> uuid.UUID | None:
    """The user behind the credential, or None for a machine one.

    A CI job or a script legitimately annotates and claims; attributing that to a fabricated
    user id would be worse than leaving it null, because the ground-truth table would then
    claim a person made a judgement they never made.
    """
    if not principal.is_user:
        return None
    try:
        return uuid.UUID(principal.id)
    except ValueError:
        return None


def _rule_out(row: OnlineEvalRule) -> RuleOut:
    return RuleOut(
        id=row.id,
        name=row.name,
        slug=row.slug,
        kind=row.kind,
        enabled=row.enabled,
        sample_rate=row.sample_rate,
        sample_group=row.sample_group,
        escalate_on_failure=row.escalate_on_failure,
        max_escalations_per_batch=row.max_escalations_per_batch,
        trace_name=row.trace_name,
        policy_version_id=row.policy_version_id,
        evaluator_version_id=row.evaluator_version_id,
        review_queue_id=row.review_queue_id,
    )


def _assignment_out(row: ReviewAssignment, evaluation: OnlineEvaluation | None) -> AssignmentOut:
    return AssignmentOut(
        id=row.id,
        queue_id=row.queue_id,
        target_type=row.target_type,
        target_id=row.target_id,
        status=row.status,
        priority=row.priority,
        reason=row.reason,
        claimed_at=row.claimed_at,
        lease_expires_at=row.lease_expires_at,
        evaluation=(
            {
                "verdict": evaluation.verdict,
                "decision_reason": evaluation.decision_reason,
                "detail": evaluation.detail,
                "error": evaluation.error,
            }
            if evaluation
            else None
        ),
    )


def _annotation_out(row: Any) -> AnnotationOut:
    return AnnotationOut(
        id=row.id,
        target_type=row.target_type,
        target_id=row.target_id,
        annotator_id=row.annotator_id,
        label=row.label,
        rating=row.rating,
        comment=row.comment,
        correction=row.correction,
        created_at=row.created_at,
    )
