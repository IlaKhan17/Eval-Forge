"""Deciding which production traces get evaluated.

Online evaluation has a cost structure that offline evaluation does not: traces arrive
continuously and forever, so "evaluate everything" is a bill that grows with traffic. This
module is the policy that keeps that bounded without letting it become arbitrary.

Three rules, each because the obvious alternative is worse:

1. **Deterministic checks run on everything.** Trajectory policies, schema checks, and
   secret scans are free and instant. Sampling them would save nothing and lose coverage
   of exactly the safety properties most worth having on every trace.

2. **Sampling is deterministic in the trace id, not random.** `random()` means a re-run
   evaluates a different subset, so re-processing a backlog spends money again and no two
   runs are comparable. Hashing the trace id makes membership a property of the trace: the
   same trace is always in or always out, replays are free, and the decision can be
   recomputed later to answer "why was this one not evaluated?".

3. **Failures escalate past the sample, but under a cap.** A trace that errored is worth
   more than a random one, so a failed trace is evaluated regardless of the sample. Left
   uncapped that is a cost bomb: an incident produces an error spike, which produces a
   judge-call spike, and the surprise bill arrives on the day you can least afford the
   distraction. Escalation therefore has its own budget.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

#: Resolution of the hash-to-unit-interval mapping. 2**32 buckets resolves a rate down to
#: about 2e-10, far finer than any sample rate anyone will configure.
_BUCKETS = 2**32

#: `capped` and `budget` are both "a limit stopped this" and are deliberately separate: `capped`
#: means this batch's escalation allowance ran out and the next batch picks the trace up, while
#: `budget` means a monthly spend ceiling is reached and nothing paid runs until it changes. One is
#: a delay, the other is a stop, and a reader chasing a coverage gap needs to know which.
Reason = Literal[
    "deterministic", "sampled", "escalated", "forced", "not_sampled", "capped", "budget"
]


@dataclass(frozen=True, slots=True)
class SamplingDecision:
    """Whether to evaluate a trace, and why.

    The reason is recorded, not just the boolean. "This trace has no judge score" is
    ambiguous between "not sampled", "escalation budget was exhausted", and "the rule was
    disabled", and those three have completely different responses.
    """

    evaluate: bool
    reason: Reason
    #: The trace's position in the unit interval, for debugging a rate that looks wrong.
    bucket: float | None = None

    @property
    def costs_money(self) -> bool:
        """Whether this decision implies a paid call.

        Deterministic evaluation is free, so it is excluded from every budget. Conflating
        the two makes a cost cap throttle the checks that cost nothing.
        """
        return self.evaluate and self.reason != "deterministic"


def bucket_of(trace_id: str, *, salt: str = "") -> float:
    """Map a trace id into [0, 1) deterministically.

    SHA-256 rather than `hash()`: Python's string hash is randomised per process by
    default, so `hash()` would make the sample non-reproducible across restarts — the
    exact property this function exists to provide.
    """
    digest = hashlib.sha256(f"{salt}\x00{trace_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / _BUCKETS


def is_sampled(trace_id: str, rate: float, *, salt: str = "") -> bool:
    """Whether a trace falls inside a sample of the given rate.

    `rate` is clamped rather than validated, because a misconfigured 1.5 should mean
    "everything" rather than crash a worker mid-batch on a value that is obviously
    intended to be permissive.
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    return bucket_of(trace_id, salt=salt) < rate


@dataclass(frozen=True, slots=True)
class SamplingRule:
    """How one online-evaluation rule selects traces."""

    #: Identifies the rule, and doubles as the default sampling salt.
    rule_id: str
    sample_rate: float = 0.01
    enabled: bool = True
    #: Free checks bypass sampling entirely.
    deterministic: bool = False
    #: Evaluate a failed trace even when it falls outside the sample.
    escalate_on_failure: bool = True
    #: Traces sharing a salt are sampled *together*. See `salt` for why this matters.
    sample_group: str | None = None

    @property
    def salt(self) -> str:
        """The sampling salt.

        Defaults to the rule id, so two rules at 1 % do not select the *same* 1 % of
        traffic. With a shared salt they would, which sounds harmless and is not: 99 % of
        traces would then be invisible to every judge in the project, and the sampled 1 %
        would be a fixed, unrepresentative cohort forever.

        Setting `sample_group` opts into the shared behaviour, which is the right choice
        when several judges must score the *same* traces so their scores are comparable
        per trace.
        """
        return self.sample_group or self.rule_id


@dataclass(slots=True)
class EscalationBudget:
    """A cap on how many failure escalations may be paid for in one window.

    Stateful and deliberately simple: the worker holds one per rule per batch. It is a
    circuit breaker, not an accountant — the guarantee wanted is "an error spike cannot
    produce an unbounded judge bill", and a precise count is not needed for that.
    """

    limit: int
    spent: int = 0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    def take(self) -> bool:
        if self.exhausted:
            return False
        self.spent += 1
        return True


def decide(  # noqa: PLR0911 — one return per documented decision, which reads better flat
    *,
    trace_id: str,
    rule: SamplingRule,
    trace_failed: bool = False,
    forced: bool = False,
    budget: EscalationBudget | None = None,
) -> SamplingDecision:
    """Decide whether one trace should be evaluated by one rule.

    Order matters. `forced` (an explicit re-run request) wins over everything so a human
    can always ask for a specific trace. Deterministic rules come next, because they are
    free and must not be gated by anything. Then the sample. Then escalation, which is
    checked last precisely so it only consumes budget for traces the sample missed.
    """
    if not rule.enabled:
        return SamplingDecision(evaluate=False, reason="not_sampled")

    if forced:
        return SamplingDecision(evaluate=True, reason="forced")

    if rule.deterministic:
        # No bucket is computed: this rule does not sample, and reporting a bucket would
        # imply it might.
        return SamplingDecision(evaluate=True, reason="deterministic")

    bucket = bucket_of(trace_id, salt=rule.salt)
    if bucket < rule.sample_rate:
        return SamplingDecision(evaluate=True, reason="sampled", bucket=bucket)

    if trace_failed and rule.escalate_on_failure:
        if budget is None or budget.take():
            return SamplingDecision(evaluate=True, reason="escalated", bucket=bucket)
        # Distinguished from "not sampled" on purpose. A capped escalation means there are
        # more failures than the budget allows, which is itself worth knowing and is a
        # reason to raise the cap or fix the failures — not the same as a trace simply
        # falling outside a 1 % sample.
        return SamplingDecision(evaluate=False, reason="capped", bucket=bucket)

    return SamplingDecision(evaluate=False, reason="not_sampled", bucket=bucket)


def expected_paid_calls(trace_count: int, rate: float, *, failure_rate: float = 0.0) -> float:
    """Estimated paid evaluations for a batch, for reporting before spending.

    Failures that fall inside the sample are not counted twice — the escalation only fires
    for traces the sample already missed, which is why the second term carries a
    `(1 - rate)` factor. Getting that wrong overstates cost by the failure rate, which
    sounds small until someone sizes a budget from it.
    """
    sampled = trace_count * max(0.0, min(1.0, rate))
    escalated = trace_count * failure_rate * (1 - max(0.0, min(1.0, rate)))
    return sampled + escalated
