"""Review queues, annotations, and promoting a failure into a dataset example.

This is the step that makes online evaluation worth having. A failure that nobody converts
into a test is a failure that recurs, so the path from "production trace violated a policy"
to "the suite now covers this case" has to be short enough that people actually walk it.

Two pieces of machinery deserve their explanation up front:

**Claiming uses `FOR UPDATE SKIP LOCKED`.** Two reviewers pressing "next" at the same instant
must get different items. The alternatives are worse in specific ways: a plain `SELECT` then
`UPDATE` hands both of them the same item, and `FOR UPDATE` without `SKIP LOCKED` makes the
second reviewer wait on a row lock held by the first — which looks like the UI hanging.

**Promotion never touches a locked dataset version.** A locked version is the anchor of every
historical comparison; its content hash is what lets a run prove it saw identical data. If
promotion could append to one, every prior experiment against it would silently become
unreproducible. So promotion targets a draft, creating one if necessary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge_api.db.models.evaluation import Dataset, DatasetVersion
from evalforge_api.db.models.online import (
    Annotation,
    OnlineEvaluation,
    ReviewAssignment,
    ReviewQueue,
)
from evalforge_api.db.models.traces import Span as SpanRow
from evalforge_api.db.models.traces import Trace as TraceRow
from evalforge_api.errors import ConflictError, NotFoundError, UnprocessableError
from evalforge_api.services.datasets import DatasetService
from evalforge_types import Example


@dataclass(frozen=True, slots=True)
class ClaimedItem:
    assignment: ReviewAssignment
    evaluation: OnlineEvaluation | None


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    dataset_version_id: uuid.UUID
    example_id: str
    created_draft: bool
    already_present: bool


class ReviewService:
    def __init__(self, session: AsyncSession, *, project_id: uuid.UUID) -> None:
        self.session = session
        self.project_id = project_id

    # -------------------------------------------------------------------- queues

    async def get_queue(self, queue_id: uuid.UUID) -> ReviewQueue:
        queue = await self.session.get(ReviewQueue, queue_id)
        if queue is None or queue.project_id != self.project_id or queue.deleted_at is not None:
            # 404 for a foreign row, never 403: a 403 confirms it exists.
            raise NotFoundError("No such review queue.")
        return queue

    async def enqueue(
        self,
        *,
        queue_id: uuid.UUID,
        target_type: str,
        target_id: str,
        priority: int = 0,
        reason: str | None = None,
        online_evaluation_id: uuid.UUID | None = None,
    ) -> ReviewAssignment | None:
        """Add an item, or return None when it is already queued.

        Already-queued is not an error. Two rules failing on one trace should produce one
        review item, not two people doing the same work.
        """
        await self.get_queue(queue_id)
        statement = (
            pg_insert(_table(ReviewAssignment))
            .values(
                project_id=self.project_id,
                queue_id=queue_id,
                target_type=target_type,
                target_id=target_id,
                priority=priority,
                reason=reason,
                online_evaluation_id=online_evaluation_id,
                status="pending",
            )
            .on_conflict_do_nothing(constraint="uq_review_assignments_target")
            .returning(_table(ReviewAssignment).c.id)
        )
        created = (await self.session.execute(statement)).scalar_one_or_none()
        if created is None:
            return None
        return await self.session.get(ReviewAssignment, created)

    # -------------------------------------------------------------------- claims

    async def claim_next(
        self,
        *,
        queue_id: uuid.UUID,
        reviewer_id: uuid.UUID | None,
        now: datetime | None = None,
    ) -> ClaimedItem | None:
        """Take the next item, or None when the queue is empty.

        Highest priority first, then oldest. Oldest rather than newest so a backlog drains
        instead of accumulating a tail nobody ever reaches.
        """
        moment = now or datetime.now(UTC)
        queue = await self.get_queue(queue_id)

        # Expired leases are reclaimed first, in the same call. Doing it here rather than in
        # a periodic job means an abandoned item is available to the very next reviewer who
        # asks, instead of waiting for a sweeper to come round.
        await self.release_expired(now=moment)

        table = _table(ReviewAssignment)
        candidate = (
            select(table.c.id)
            .where(
                table.c.project_id == self.project_id,
                table.c.queue_id == queue_id,
                table.c.status == "pending",
            )
            .order_by(table.c.priority.desc(), table.c.created_at)
            .limit(1)
            # The whole point. Without SKIP LOCKED the second concurrent reviewer blocks on
            # the first's row lock; without FOR UPDATE they both claim the same row.
            .with_for_update(skip_locked=True)
        )

        claimed = (
            await self.session.execute(
                update(table)
                .where(table.c.id == candidate.scalar_subquery())
                .values(
                    status="in_review",
                    assignee_id=reviewer_id,
                    claimed_at=moment,
                    lease_expires_at=moment + timedelta(seconds=queue.lease_seconds),
                )
                .returning(table.c.id)
            )
        ).scalar_one_or_none()

        if claimed is None:
            return None

        assignment = await self.session.get(ReviewAssignment, claimed)
        assert assignment is not None
        evaluation = (
            await self.session.get(OnlineEvaluation, assignment.online_evaluation_id)
            if assignment.online_evaluation_id
            else None
        )
        return ClaimedItem(assignment=assignment, evaluation=evaluation)

    async def release_expired(self, *, now: datetime | None = None) -> int:
        """Return abandoned claims to the pool.

        A reviewer who claims an item and closes their laptop must not hold it forever. The
        claim is cleared rather than reassigned, because guessing who should get it next is
        the claim query's job.
        """
        moment = now or datetime.now(UTC)
        table = _table(ReviewAssignment)
        result = await self.session.execute(
            update(table)
            .where(
                table.c.project_id == self.project_id,
                table.c.status == "in_review",
                table.c.lease_expires_at.is_not(None),
                table.c.lease_expires_at < moment,
            )
            .values(status="pending", assignee_id=None, claimed_at=None, lease_expires_at=None)
            .returning(table.c.id)
        )
        # `returning` rather than `rowcount`: the async result type does not expose a
        # rowcount, and counting the returned ids is exact rather than driver-dependent.
        return len(result.scalars().all())

    async def complete(
        self,
        assignment_id: uuid.UUID,
        *,
        status: str = "done",
        now: datetime | None = None,
    ) -> ReviewAssignment:
        if status not in ("done", "skipped"):
            msg = f"a reviewer can only finish an item as 'done' or 'skipped', not {status!r}"
            raise UnprocessableError(msg)

        assignment = await self.session.get(ReviewAssignment, assignment_id)
        if assignment is None or assignment.project_id != self.project_id:
            raise NotFoundError("No such review assignment.")
        if assignment.status in ("done", "skipped"):
            # Idempotent rather than an error: a double-submitted form should not look like
            # a failure to the reviewer who submitted it.
            return assignment

        assignment.status = status
        assignment.completed_at = now or datetime.now(UTC)
        assignment.lease_expires_at = None
        await self.session.flush()
        return assignment

    async def queue_depth(self, queue_id: uuid.UUID) -> dict[str, int]:
        rows = (
            await self.session.execute(
                select(ReviewAssignment.status, func.count())
                .where(
                    ReviewAssignment.project_id == self.project_id,
                    ReviewAssignment.queue_id == queue_id,
                )
                .group_by(ReviewAssignment.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}

    # --------------------------------------------------------------- annotations

    async def annotate(
        self,
        *,
        target_type: str,
        target_id: str,
        annotator_id: uuid.UUID | None,
        label: str | None = None,
        rating: float | None = None,
        comment: str | None = None,
        correction: dict[str, Any] | None = None,
        preference_target_id: str | None = None,
        preference_winner: str | None = None,
    ) -> Annotation:
        """Record a human judgement.

        Never written by a model. This table is the ground truth that judge calibration is
        measured against, and labelling it with an LLM makes the whole exercise circular.
        """
        if not any(
            value is not None for value in (label, rating, comment, correction, preference_winner)
        ):
            msg = (
                "an annotation needs a label, rating, comment, correction, or preference. "
                "An empty annotation would count as a label in the ground-truth table."
            )
            raise UnprocessableError(msg)

        row = Annotation(
            project_id=self.project_id,
            target_type=target_type,
            target_id=target_id,
            annotator_id=annotator_id,
            label=label,
            rating=rating,
            comment=comment,
            correction=correction,
            preference_target_id=preference_target_id,
            preference_winner=preference_winner,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def annotations_for(self, *, target_type: str, target_id: str) -> list[Annotation]:
        return list(
            (
                await self.session.execute(
                    select(Annotation)
                    .where(
                        Annotation.project_id == self.project_id,
                        Annotation.target_type == target_type,
                        Annotation.target_id == target_id,
                    )
                    .order_by(Annotation.created_at)
                )
            )
            .scalars()
            .all()
        )

    # ---------------------------------------------------------------- promotion

    async def promote_trace(
        self,
        *,
        trace_id: str,
        dataset_slug: str,
        expected: dict[str, Any] | None = None,
        annotation_id: uuid.UUID | None = None,
        input_from_span: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PromotionOutcome:
        """Turn a production trace into a dataset example.

        The single most valuable operation in the product: it is how a real failure becomes a
        regression test. Three rules make it safe.

        **It targets a draft, never a locked version.** A locked version's content hash is
        what lets an experiment prove it saw identical data; appending to one would silently
        invalidate every historical comparison against it. A draft is created if none exists.

        **`expected` comes from a human.** Either passed directly or taken from an
        annotation's `correction`. Promoting a trace with the *model's own output* as the
        expected answer would enshrine the bug as the specification — the failure mode that
        makes a golden dataset actively harmful.

        **The example records where it came from.** `source_trace_id` survives into the
        dataset, so an example that later looks wrong can be traced back to the production
        interaction that produced it.
        """
        trace = (
            await self.session.execute(
                select(TraceRow).where(
                    TraceRow.project_id == self.project_id, TraceRow.trace_id == trace_id
                )
            )
        ).scalar_one_or_none()
        if trace is None:
            raise NotFoundError("No such trace.")

        resolved = dict(expected) if expected else None
        if resolved is None and annotation_id is not None:
            annotation = await self.session.get(Annotation, annotation_id)
            if annotation is None or annotation.project_id != self.project_id:
                raise NotFoundError("No such annotation.")
            resolved = dict(annotation.correction) if annotation.correction else None
            if resolved is None and annotation.label is not None:
                resolved = {"label": annotation.label}

        if resolved is None:
            # Refused rather than defaulted to the model's output. An example whose expected
            # answer is what the model already did teaches the suite to accept the bug.
            msg = (
                "promotion needs an expected result from a human: pass `expected`, or an "
                "`annotation_id` whose annotation carries a correction or a label. "
                "Promoting the model's own output would make the defect the specification."
            )
            raise UnprocessableError(msg)

        dataset = await self._dataset(dataset_slug)
        version, created_draft = await self._draft_version(dataset)

        example = Example(
            id=f"trace-{trace_id}",
            input=await self._input_for(trace, span_id=input_from_span),
            expected=resolved,
            metadata={
                "promoted_from": "trace",
                "trace_name": trace.name,
                "trace_status": trace.status,
                **(metadata or {}),
            },
            source_trace_id=trace_id,
        )

        service = DatasetService(self.session, project_id=self.project_id)
        try:
            await service.append_examples(version.id, [example])
        except ConflictError:
            # Already promoted. Idempotent on purpose: a reviewer who clicks twice, or two
            # reviewers who both promote the same trace, should not get an error and should
            # not create a duplicate example — duplicates skew every metric computed over
            # the dataset.
            return PromotionOutcome(
                dataset_version_id=version.id,
                example_id=example.id,
                created_draft=created_draft,
                already_present=True,
            )

        return PromotionOutcome(
            dataset_version_id=version.id,
            example_id=example.id,
            created_draft=created_draft,
            already_present=False,
        )

    async def _dataset(self, slug: str) -> Dataset:
        dataset = (
            await self.session.execute(
                select(Dataset).where(
                    Dataset.project_id == self.project_id,
                    Dataset.slug == slug,
                    Dataset.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if dataset is None:
            raise NotFoundError(f"No dataset with slug {slug!r}.")
        return dataset

    async def _draft_version(self, dataset: Dataset) -> tuple[DatasetVersion, bool]:
        """The dataset's open draft, created if there is none."""
        draft = (
            await self.session.execute(
                select(DatasetVersion)
                .where(
                    DatasetVersion.dataset_id == dataset.id,
                    DatasetVersion.status == "draft",
                )
                .order_by(DatasetVersion.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if draft is not None:
            return draft, False

        highest = (
            await self.session.execute(
                select(func.count())
                .select_from(DatasetVersion)
                .where(DatasetVersion.dataset_id == dataset.id)
            )
        ).scalar_one()
        draft = DatasetVersion(
            project_id=self.project_id,
            dataset_id=dataset.id,
            version=f"v{int(highest) + 1}",
            status="draft",
        )
        self.session.add(draft)
        await self.session.flush()
        return draft, True

    async def _input_for(self, trace: TraceRow, *, span_id: str | None) -> dict[str, Any]:
        """The example's input: a named span's input, or the root span's.

        Falling back to the trace's metadata rather than to an empty dict, because an example
        with no input is an example that cannot be re-run — and an unrunnable example in a
        golden dataset is worse than a missing one, since it counts toward coverage.
        """
        statement = select(SpanRow).where(
            SpanRow.project_id == self.project_id, SpanRow.trace_id == trace.trace_id
        )
        if span_id:
            statement = statement.where(SpanRow.span_id == span_id)
        else:
            statement = statement.where(SpanRow.parent_span_id.is_(None))
        span = (
            await self.session.execute(statement.order_by(SpanRow.started_at).limit(1))
        ).scalar_one_or_none()

        if span is not None and isinstance(span.input_inline, dict):
            return dict(span.input_inline)
        if span is not None and span.input_inline is not None:
            return {"input": span.input_inline}
        if trace.trace_metadata:
            return dict(trace.trace_metadata)
        return {}


async def pending_review_counts(session: AsyncSession, *, project_id: uuid.UUID) -> dict[str, int]:
    """Pending items per queue slug, for a dashboard header."""
    rows = (
        await session.execute(
            select(ReviewQueue.slug, func.count(ReviewAssignment.id))
            .join(ReviewAssignment, ReviewAssignment.queue_id == ReviewQueue.id)
            .where(
                ReviewQueue.project_id == project_id,
                ReviewQueue.deleted_at.is_(None),
                or_(
                    ReviewAssignment.status == "pending",
                    ReviewAssignment.status == "in_review",
                ),
            )
            .group_by(ReviewQueue.slug)
        )
    ).all()
    return {str(slug): int(count) for slug, count in rows}


def _table(model: type[Any]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


__all__ = [
    "ClaimedItem",
    "PromotionOutcome",
    "ReviewService",
    "pending_review_counts",
]
