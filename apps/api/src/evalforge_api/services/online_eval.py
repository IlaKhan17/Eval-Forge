"""Evaluating production traces as they arrive.

The offline engine answers "is this change worse than main?". This answers "is what we
shipped actually behaving?" — and the two share the evaluation code deliberately, because a
policy that passes in CI and is never checked in production is a policy nobody has verified.

Cost is the constraint that shapes everything here. Offline runs are bounded by the dataset;
online runs are bounded by traffic, which is to say unbounded. So:

- trajectory policies and other deterministic checks run on **every** trace, because they
  are free and they cover the safety properties most worth having everywhere
- judges run on a **deterministic sample** (`evalforge_core.sampling`), so replaying a
  backlog costs nothing extra and coverage is reproducible
- failures escalate past the sample under a **cap**, so an incident cannot turn an error
  spike into a surprise bill

Everything is idempotent. A worker that dies mid-batch and restarts must not double-count,
because an online metric that drifts upward on every replay is worse than no metric at all.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge_api.db.models.evaluation import TrajectoryPolicyVersion
from evalforge_api.db.models.online import (
    OnlineEvalRule,
    OnlineEvaluation,
    ReviewAssignment,
    ReviewQueue,
)
from evalforge_api.db.models.traces import Span as SpanRow
from evalforge_api.db.models.traces import Trace as TraceRow
from evalforge_api.services import budget as budget_service
from evalforge_core.sampling import EscalationBudget, SamplingDecision, SamplingRule, decide
from evalforge_trajectory import PolicyError, evaluate_policy, load_policy
from evalforge_types import Span, SpanType, Status, TokenUsage, Trace

logger = logging.getLogger("evalforge.online_eval")

#: How many traces one batch will consider. Bounded so a backlog is drained in steady
#: increments rather than in one transaction that holds locks for minutes.
DEFAULT_BATCH_SIZE = 200


@dataclass(slots=True)
class BatchOutcome:
    """What one worker batch did, for logging and for the tests."""

    traces_considered: int = 0
    evaluations_written: int = 0
    skipped: int = 0
    failures: int = 0
    errors: int = 0
    queued_for_review: int = 0
    cost: Decimal = Decimal(0)
    reasons: dict[str, int] = field(default_factory=dict)
    #: True when the project's monthly ceiling stopped paid rules in this batch. Surfaced so the
    #: worker's log line and the API's run report say *why* a quiet batch was quiet.
    budget_exhausted: bool = False

    def record(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class OnlineEvalService:
    def __init__(self, session: AsyncSession, *, project_id: uuid.UUID) -> None:
        self.session = session
        self.project_id = project_id

    # ------------------------------------------------------------------ loading

    async def active_rules(self) -> list[OnlineEvalRule]:
        return list(
            (
                await self.session.execute(
                    select(OnlineEvalRule).where(
                        OnlineEvalRule.project_id == self.project_id,
                        OnlineEvalRule.enabled.is_(True),
                        OnlineEvalRule.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    def _pending_query(self, rule: OnlineEvalRule, *, since: datetime, limit: int) -> Select[Any]:
        """Traces this rule has not yet recorded a decision for.

        A `NOT EXISTS` against `online_evaluations` rather than a high-water mark on time.
        A timestamp cursor looks cheaper and is wrong: ingestion is not ordered by
        `started_at` — a mobile client can upload a trace hours late — so a cursor would
        skip every late arrival permanently.
        """
        already = select(OnlineEvaluation.id).where(
            OnlineEvaluation.project_id == self.project_id,
            OnlineEvaluation.rule_id == rule.id,
            OnlineEvaluation.trace_id == TraceRow.trace_id,
        )
        statement = select(TraceRow).where(
            TraceRow.project_id == self.project_id,
            TraceRow.started_at >= since,
            ~already.exists(),
        )
        if rule.trace_name:
            statement = statement.where(TraceRow.name == rule.trace_name)
        if rule.environment_id:
            statement = statement.where(TraceRow.environment_id == rule.environment_id)
        # Oldest first: a backlog should drain in arrival order, so the queue a human sees
        # is not dominated by whatever happens to be newest.
        return statement.order_by(TraceRow.started_at).limit(limit)

    # ------------------------------------------------------------------- running

    @staticmethod
    def _costs_money(rule: OnlineEvalRule) -> bool:
        """Whether running this rule can produce a provider bill.

        Kind, not configuration: a trajectory policy is deterministic code over a stored trace and
        costs nothing however it is set up, while a judge rule bills per call. That distinction is
        what lets a budget stop the expensive controls without stopping the cheap ones.
        """
        return rule.kind in ("llm_judge",)

    async def run_batch(
        self,
        *,
        since: datetime | None = None,
        limit: int = DEFAULT_BATCH_SIZE,
        rules: list[OnlineEvalRule] | None = None,
        now: datetime | None = None,
    ) -> BatchOutcome:
        """Apply every active rule to the traces it has not yet seen."""
        moment = now or datetime.now(UTC)
        window_start = since or (moment - timedelta(days=1))
        outcome = BatchOutcome()

        # Once per batch, not once per trace. The query is a bounded sum and the answer cannot
        # change materially inside one batch — checking per trace would multiply the cost of the
        # check by exactly the volume the check exists to bound.
        spend = await budget_service.status(self.session, project_id=self.project_id, now=moment)
        outcome.budget_exhausted = spend.exhausted
        if spend.exhausted:
            logger.warning(
                "project %s has reached its monthly limit of %s (spent %s); paid rules are "
                "skipped until the month turns or the limit rises",
                self.project_id,
                spend.limit,
                spend.spent,
            )
        elif spend.warning:
            logger.warning(
                "project %s has used %.0f%% of its monthly limit of %s",
                self.project_id,
                (spend.ratio or 0) * 100,
                spend.limit,
            )

        for rule in rules if rules is not None else await self.active_rules():
            traces = list(
                (
                    await self.session.execute(
                        self._pending_query(rule, since=window_start, limit=limit)
                    )
                )
                .scalars()
                .all()
            )
            outcome.traces_considered += len(traces)
            # One budget per rule per batch. Shared across rules it would let a noisy rule
            # starve a quiet one; unbounded it would let an error spike spend without limit.
            budget = EscalationBudget(limit=rule.max_escalations_per_batch)

            # A budget stops what is billable and nothing else. Switching off a free deterministic
            # policy because the judge allowance ran out would trade a bill for an incident — the
            # cheap safety controls have to keep running exactly when the expensive ones stop.
            paid_and_broke = spend.exhausted and self._costs_money(rule)

            for trace in traces:
                await self._apply(
                    rule,
                    trace,
                    budget=budget,
                    outcome=outcome,
                    now=moment,
                    over_budget=paid_and_broke,
                )

        return outcome

    async def _apply(
        self,
        rule: OnlineEvalRule,
        trace: TraceRow,
        *,
        budget: EscalationBudget,
        outcome: BatchOutcome,
        now: datetime,
        forced: bool = False,
        over_budget: bool = False,
    ) -> None:
        if over_budget:
            # Recorded, not dropped. A month where nothing was judged and nothing says why is
            # indistinguishable from a month where nothing needed judging, and the second is the
            # story people tell themselves.
            await self._write(
                rule,
                trace,
                verdict="skipped",
                decision=SamplingDecision(evaluate=False, reason="budget"),
                detail={"note": "the project's monthly spend limit is reached"},
                now=now,
            )
            outcome.record("budget")
            outcome.skipped += 1
            return

        decision = decide(
            trace_id=trace.trace_id,
            rule=_sampling_rule(rule),
            trace_failed=trace.status != "ok" or trace.error_count > 0,
            forced=forced,
            budget=budget,
        )
        outcome.record(decision.reason)

        if not decision.evaluate:
            # A skip is recorded, not dropped. Otherwise "no score" cannot be told apart
            # from "not sampled", "budget exhausted", or "the worker never got here", and
            # coverage becomes something you infer rather than something you can query.
            await self._write(
                rule,
                trace,
                verdict="skipped",
                decision=decision,
                detail={},
                now=now,
            )
            outcome.skipped += 1
            return

        try:
            verdict, detail, score, cost = await self._evaluate(rule, trace)
            error: str | None = None
        except Exception as exc:
            # An evaluation that broke is an error, never a failing trace. Recording it as
            # a failure would turn a provider outage or a malformed policy into a fake
            # quality regression, and would queue innocent traces for human review.
            verdict, detail, score, cost = "error", {}, None, Decimal(0)
            error = f"{type(exc).__name__}: {exc}"

        written = await self._write(
            rule,
            trace,
            verdict=verdict,
            decision=decision,
            detail=detail,
            score=score,
            cost=cost,
            error=error,
            now=now,
        )
        if written is None:
            # Another worker got there first. The unique constraint is the arbiter, and
            # losing the race is a normal outcome rather than an error.
            return

        outcome.evaluations_written += 1
        outcome.cost += cost
        if verdict == "error":
            outcome.errors += 1
        elif verdict == "fail":
            outcome.failures += 1
            if await self._enqueue(rule, trace, evaluation_id=written, detail=detail):
                outcome.queued_for_review += 1

    async def _evaluate(
        self, rule: OnlineEvalRule, trace: TraceRow
    ) -> tuple[str, dict[str, Any], float | None, Decimal]:
        if rule.kind == "trajectory":
            return await self._evaluate_trajectory(rule, trace)
        # Judges and deterministic evaluators need the evaluator registry and, for judges, a
        # provider client. Both are the worker's to supply; until it does, saying so beats
        # silently recording a pass.
        msg = (
            f"online rule kind {rule.kind!r} is not implemented yet; only 'trajectory' "
            "rules run online today"
        )
        raise NotImplementedError(msg)

    async def _evaluate_trajectory(
        self, rule: OnlineEvalRule, trace: TraceRow
    ) -> tuple[str, dict[str, Any], float | None, Decimal]:
        version = await self.session.get(TrajectoryPolicyVersion, rule.policy_version_id)
        if version is None or version.project_id != self.project_id:
            msg = f"rule {rule.slug!r} points at a policy version that does not exist"
            raise PolicyError(msg)

        # The stored YAML source, not the parsed form: the parser's errors carry line
        # numbers, and a line number the policy author will recognise is worth more than
        # skipping a re-parse that takes microseconds.
        policy = load_policy(version.source_yaml, path=f"policy version {version.version}")
        domain = await self.load_trace(trace)
        result = evaluate_policy(policy, domain)

        detail: dict[str, Any] = {
            "policy": policy.policy.name,
            "policy_version": version.version,
            "failures": [
                {
                    "rule_id": failure.rule_id,
                    "kind": failure.rule_kind,
                    "message": failure.message,
                    "span_id": failure.offending_span_id,
                    "offending_action": failure.offending_action,
                    "severity": failure.severity.value,
                    "policy_line": failure.policy_line,
                }
                for failure in result.failures
            ],
            "inconclusive_rules": list(result.inconclusive_rules),
            "warnings": list(result.warnings),
            "incomplete": result.incomplete,
        }

        # Three outcomes, not two. A trace whose spans were dropped cannot support a claim
        # about what did *not* happen, so a `required_action` rule over it is unknown —
        # neither a pass nor a violation.
        #
        # The distinction is load-bearing here in a way it is not offline. Calling it a
        # failure would queue an innocent trace for human review, and a review queue full
        # of "your exporter dropped spans" items is a queue people stop reading. Calling it
        # a pass would hide a coverage gap behind a green number.
        if result.blocking_failures:
            verdict = "fail"
        elif result.inconclusive_rules:
            verdict = "inconclusive"
            detail["note"] = (
                "the trace was incomplete, so these rules could not be decided. This is not "
                "a policy violation; it means the trace did not carry enough information to "
                "check. Raise the SDK queue size if it recurs."
            )
        else:
            verdict = "pass"

        score = {"pass": 1.0, "fail": 0.0}.get(verdict)
        return verdict, detail, score, Decimal(0)

    # ------------------------------------------------------------------ writing

    async def _write(
        self,
        rule: OnlineEvalRule,
        trace: TraceRow,
        *,
        verdict: str,
        decision: SamplingDecision,
        detail: dict[str, Any],
        score: float | None = None,
        cost: Decimal = Decimal(0),
        error: str | None = None,
        now: datetime,
    ) -> uuid.UUID | None:
        """Insert one evaluation, or return None if one already exists.

        `ON CONFLICT DO NOTHING` on the natural key rather than a read-then-write. Two
        workers processing overlapping batches is the expected case, and a check-then-insert
        would race between the two statements.
        """
        statement = (
            pg_insert(_table(OnlineEvaluation))
            .values(
                project_id=self.project_id,
                trace_id=trace.trace_id,
                rule_id=rule.id,
                verdict=verdict,
                decision_reason=decision.reason,
                score=score,
                detail=detail,
                error=error,
                cost=cost,
                trace_started_at=trace.started_at,
                created_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_online_evaluations_trace_rule")
            .returning(_table(OnlineEvaluation).c.id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def _enqueue(
        self,
        rule: OnlineEvalRule,
        trace: TraceRow,
        *,
        evaluation_id: uuid.UUID,
        detail: dict[str, Any],
    ) -> bool:
        """Put a failing trace in front of a human, if the rule names a queue."""
        if rule.review_queue_id is None:
            return False

        failures = detail.get("failures") or []
        reason = (
            failures[0].get("message")
            if failures and isinstance(failures[0], dict)
            else f"{rule.name} failed"
        )
        statement = (
            pg_insert(_table(ReviewAssignment))
            .values(
                project_id=self.project_id,
                queue_id=rule.review_queue_id,
                target_type="trace",
                target_id=trace.trace_id,
                online_evaluation_id=evaluation_id,
                status="pending",
                # Errored traces first: a trace that both failed a policy and crashed is
                # more informative than one that only failed a policy.
                priority=10 if trace.error_count else 5,
                reason=reason,
            )
            # Already queued is success, not a conflict: two rules failing on one trace
            # should produce one review item, not two people doing the same work.
            .on_conflict_do_nothing(constraint="uq_review_assignments_target")
            .returning(_table(ReviewAssignment).c.id)
        )
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    # ------------------------------------------------------------------ loading

    async def load_trace(self, trace: TraceRow) -> Trace:
        """Rebuild the domain `Trace` the trajectory engine evaluates.

        Payload bodies are deliberately not fetched. Trajectory rules reason about actions,
        ordering, and state — never about the contents of a prompt — so loading payloads
        would add an object-store round trip per span to compute exactly the same verdict.
        """
        rows = list(
            (
                await self.session.execute(
                    select(SpanRow)
                    .where(
                        SpanRow.project_id == self.project_id,
                        SpanRow.trace_id == trace.trace_id,
                    )
                    .order_by(SpanRow.started_at, SpanRow.sequence_index)
                )
            )
            .scalars()
            .all()
        )

        return Trace(
            trace_id=trace.trace_id,
            name=trace.name,
            status=_status(trace.status),
            started_at=trace.started_at,
            ended_at=trace.ended_at,
            metadata=trace.trace_metadata,
            tags={k: str(v) for k, v in (trace.tags or {}).items()},
            state=trace.state,
            git_commit=trace.git_commit,
            dropped_span_count=trace.dropped_span_count,
            spans=[_span(row) for row in rows],
        )


def _table(model: type[Any]) -> Table:
    """The mapped Table, typed.

    Core inserts are used rather than the ORM because `ON CONFLICT DO NOTHING` has no ORM
    equivalent, and because the ORM would resolve a column literally named `metadata` to
    SQLAlchemy's own `MetaData`. `__table__` is declared as `FromClause` on the declarative
    base, so it needs narrowing for the insert construct to typecheck.
    """
    table = model.__table__
    assert isinstance(table, Table)
    return table


def _sampling_rule(rule: OnlineEvalRule) -> SamplingRule:
    return SamplingRule(
        rule_id=str(rule.id),
        sample_rate=rule.sample_rate,
        enabled=rule.enabled,
        # Trajectory and deterministic checks are free, so they are never sampled.
        deterministic=rule.kind in ("trajectory", "deterministic"),
        escalate_on_failure=rule.escalate_on_failure,
        sample_group=rule.sample_group,
    )


def _status(value: str) -> Status:
    try:
        return Status(value)
    except ValueError:
        return Status.UNSET


def _span(row: SpanRow) -> Span:
    tokens = (
        TokenUsage(prompt=row.prompt_tokens or 0, completion=row.completion_tokens or 0)
        if (row.prompt_tokens or row.completion_tokens)
        else None
    )
    return Span(
        span_id=row.span_id,
        trace_id=row.trace_id,
        parent_span_id=row.parent_span_id,
        name=row.name,
        span_type=SpanType(row.span_type),
        status=_status(row.status),
        status_message=row.status_message,
        started_at=row.started_at,
        ended_at=row.ended_at,
        attributes=row.attributes or {},
        input=row.input_inline,
        output=row.output_inline,
        model=row.model,
        provider=row.provider,
        tokens=tokens,
        cost=row.cost,
        tool_name=row.tool_name,
        tool_args=row.args_inline if isinstance(row.args_inline, dict) else None,
        error_type=row.error_type,
        events=[],
    )


async def coverage(
    session: AsyncSession, *, project_id: uuid.UUID, rule_id: uuid.UUID, since: datetime
) -> dict[str, int]:
    """How many traces each decision reason accounts for.

    The number that makes online evaluation auditable. "97 % of traces were not sampled" and
    "97 % of traces were never processed" look identical from a metric alone, and only one of
    them means the worker is behind.
    """
    rows = (
        await session.execute(
            select(OnlineEvaluation.decision_reason, func.count())
            .where(
                OnlineEvaluation.project_id == project_id,
                OnlineEvaluation.rule_id == rule_id,
                OnlineEvaluation.created_at >= since,
            )
            .group_by(OnlineEvaluation.decision_reason)
        )
    ).all()
    return {str(reason): int(count) for reason, count in rows}


async def unprocessed_count(
    session: AsyncSession, *, project_id: uuid.UUID, rule_id: uuid.UUID, since: datetime
) -> int:
    """Traces with no decision recorded — the worker's backlog for one rule."""
    already = select(OnlineEvaluation.id).where(
        OnlineEvaluation.project_id == project_id,
        OnlineEvaluation.rule_id == rule_id,
        OnlineEvaluation.trace_id == TraceRow.trace_id,
    )
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(TraceRow)
                .where(
                    TraceRow.project_id == project_id,
                    TraceRow.started_at >= since,
                    ~already.exists(),
                )
            )
        ).scalar_one()
    )


__all__ = [
    "BatchOutcome",
    "OnlineEvalService",
    "ReviewQueue",
    "coverage",
    "unprocessed_count",
]
