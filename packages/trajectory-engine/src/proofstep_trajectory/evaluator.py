"""Adapter exposing a policy as an evaluator.

Deliberately placed here rather than in `proofstep-core`. The two packages are
siblings in the layering contract and neither may import the other; this works
because `Evaluator` is a *structural* protocol, so an object with the right shape
satisfies it without any import. The only shared dependency is `proofstep-types`.

That is the payoff of protocol-based extension points: the integration costs nothing
architecturally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from proofstep_trajectory.engine import evaluate_policy
from proofstep_trajectory.parser import LoadedPolicy, load_policy_file
from proofstep_types import Score, Severity


class TrajectoryEvaluator:
    """Scores an example by evaluating its captured trajectory against a policy.

    Emits 1.0 when the policy holds and 0.0 when a blocking rule fails. An
    incomplete trajectory produces an *errored* score rather than a zero: the run
    failed to observe enough to judge, which is a different fact from observing a
    violation, and gating on the difference is the whole point.
    """

    version = 1

    def __init__(
        self,
        policy: LoadedPolicy | str | Path,
        *,
        name: str | None = None,
        fail_on_warnings: bool = False,
    ) -> None:
        self.loaded = policy if isinstance(policy, LoadedPolicy) else load_policy_file(policy)
        self.name = name or self.loaded.policy.name.replace("-", "_")
        self.fail_on_warnings = fail_on_warnings
        self.requires_trace = True

    async def evaluate(self, ctx: Any) -> Score:
        trace = getattr(ctx, "trace", None)
        if trace is None:
            return Score.failure(
                self.name,
                f"policy {self.loaded.policy.name!r} needs a captured trajectory but the "
                "task produced no trace; is the task instrumented with the SDK?",
            )

        result = evaluate_policy(self.loaded, trace)

        if result.inconclusive_rules:
            return Score.failure(
                self.name,
                f"trajectory is incomplete, so {len(result.inconclusive_rules)} rule(s) "
                f"could not be judged: {', '.join(result.inconclusive_rules)}",
            )

        failing = [
            f
            for f in result.failures
            if f.severity is Severity.BLOCK or (self.fail_on_warnings and f.blocking is False)
        ]
        passed = not failing

        return Score(
            metric=self.name,
            value=1.0 if passed else 0.0,
            passed=passed,
            reasoning=result.format(policy_path=self.loaded.path) if failing else None,
            raw={
                "policy": self.loaded.policy.name,
                "policy_hash": self.loaded.content_hash,
                "event_count": len(result.events),
                "failures": [
                    {
                        "rule_id": f.rule_id,
                        "rule_kind": f.rule_kind,
                        "severity": f.severity.value,
                        "message": f.message,
                        "span_id": f.offending_span_id,
                        "event_index": f.offending_event_index,
                        "policy_line": f.policy_line,
                    }
                    for f in result.failures
                ],
                "warnings": result.warnings,
            },
        )
