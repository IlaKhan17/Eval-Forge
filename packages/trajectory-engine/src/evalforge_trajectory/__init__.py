"""EvalForge trajectory policy engine (pure library — no I/O).

Policy-as-code for tool-using agents: `(policy, trace) -> list[PolicyFailure]`.

Output evaluation asks whether the answer was good. This asks whether the agent
behaved legitimately on the way there — an agent can produce a flawless email and
still have sent it before approval, which no output evaluator can detect.
"""

from evalforge_trajectory.engine import evaluate_policy
from evalforge_trajectory.events import (
    EventRef,
    PolicyFailure,
    PolicyResult,
    TrajectoryEvent,
)
from evalforge_trajectory.normalize import Normalized, args_hash, normalize
from evalforge_trajectory.parser import (
    LoadedPolicy,
    PolicyError,
    check_actions,
    load_policy,
    load_policy_file,
)
from evalforge_trajectory.predicates import PredicateError, compile_predicate
from evalforge_trajectory.schema import Include, Policy

__version__ = "0.1.0.dev0"

__all__ = [
    "EventRef",
    "Include",
    "LoadedPolicy",
    "Normalized",
    "Policy",
    "PolicyError",
    "PolicyFailure",
    "PolicyResult",
    "PredicateError",
    "TrajectoryEvent",
    "args_hash",
    "check_actions",
    "compile_predicate",
    "evaluate_policy",
    "load_policy",
    "load_policy_file",
    "normalize",
]
