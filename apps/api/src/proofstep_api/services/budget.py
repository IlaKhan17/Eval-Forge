"""A monthly ceiling on what the server will spend on a project's behalf.

Suites already carry `max_cost`, which stops one run. Nothing stopped the *sum* of runs, and the
spend that accumulates without anyone starting it is the online-evaluation loop: a judge rule at a
1% sample on a busy service bills continuously, quietly, and forever. A limit that exists per
invocation and not per month is not a budget.

## What this can and cannot stop

Only spend the **server initiates** — online evaluation. A judge the CLI calls runs in the user's
own process against their own provider account; the server records what it is told and has no way to
refuse it. Saying that plainly matters, because a "spend limit" that silently covers half the spend
is worse than one whose scope is stated: someone would trust it for the half it does not cover.

## What exhaustion does, and does not do

It stops paid rules. It does **not** stop free ones — a deterministic trajectory policy costs
nothing per trace, and switching off the safety checks because the judge budget ran out would trade
a bill for an incident. That asymmetry is the whole design: the cheap controls keep running exactly
when the expensive ones stop.

Every skipped evaluation is recorded with `decision_reason = 'budget'`, so a gap in coverage is
visible as a reason rather than as an absence. A month where nothing was judged and nothing says why
is indistinguishable from a month where nothing needed judging.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from proofstep_api.db.models.identity import Project
from proofstep_api.db.models.online import OnlineEvaluation

#: Fraction of the limit at which a deployment should be told it is approaching the ceiling. Warning
#: only at 100% is warning after the fact — the useful moment is while there is still room to raise
#: the limit or turn a rule down.
WARN_RATIO = 0.8


@dataclass(frozen=True)
class BudgetStatus:
    """Where a project stands this calendar month."""

    #: None means unlimited. Distinct from 0, which means "spend nothing" — a real configuration for
    #: a project that should run only its free deterministic rules.
    limit: Decimal | None
    spent: Decimal
    month_start: datetime

    @property
    def unlimited(self) -> bool:
        return self.limit is None

    @property
    def remaining(self) -> Decimal | None:
        return None if self.limit is None else max(Decimal(0), self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.spent >= self.limit

    @property
    def ratio(self) -> float | None:
        """Share of the limit used, or None when unlimited.

        `None` rather than 0.0 for an unlimited project: a gauge reading zero would look like a
        project spending nothing, which is a different fact from one that cannot be over budget.
        """
        if self.limit is None or self.limit == 0:
            return None
        return float(self.spent / self.limit)

    @property
    def warning(self) -> bool:
        ratio = self.ratio
        return ratio is not None and ratio >= WARN_RATIO and not self.exhausted


def month_start(now: datetime | None = None) -> datetime:
    """First instant of the current UTC calendar month.

    Calendar month, not a rolling 30 days, because a budget is a thing people reconcile against an
    invoice — and every provider bills by the calendar.
    """
    moment = now or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def status(
    session: AsyncSession, *, project_id: uuid.UUID, now: datetime | None = None
) -> BudgetStatus:
    """Month-to-date server-side spend for one project, against its limit."""
    start = month_start(now)
    project = await session.get(Project, project_id)
    limit = project.monthly_cost_limit if project is not None else None

    # Summed from the evaluations themselves rather than from a running counter. A counter drifts —
    # a retried batch, a rolled-back transaction — and a budget that drifts either refuses spend
    # that never happened or allows spend that did. The query is bounded by the
    # (project_id, created_at) index and runs once per batch, not once per evaluation.
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(OnlineEvaluation.cost), 0)).where(
                OnlineEvaluation.project_id == project_id,
                OnlineEvaluation.created_at >= start,
            )
        )
    ).scalar_one()

    return BudgetStatus(limit=limit, spent=Decimal(str(spent)), month_start=start)


__all__ = ["WARN_RATIO", "BudgetStatus", "month_start", "status"]
