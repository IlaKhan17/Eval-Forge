"""Proofstep trajectory policy engine (pure library — no I/O).

Policy-as-code for tool-using agents: `(policy, trace) -> list[PolicyFailure]`.

Output evaluation asks whether the answer was good. This asks whether the agent
behaved legitimately on the way there — an agent can produce a flawless email and
still have sent it before approval, which no output evaluator can detect.
"""

from importlib import metadata as _metadata

from proofstep_trajectory.engine import evaluate_policy
from proofstep_trajectory.events import (
    EventRef,
    PolicyFailure,
    PolicyResult,
    TrajectoryEvent,
)
from proofstep_trajectory.normalize import Normalized, args_hash, normalize
from proofstep_trajectory.parser import (
    LoadedPolicy,
    PolicyError,
    check_actions,
    load_policy,
    load_policy_file,
)
from proofstep_trajectory.predicates import PredicateError, compile_predicate
from proofstep_trajectory.schema import Include, Policy

# Read from the installed distribution rather than written here twice. A hand-maintained
# copy drifts the first time a release bumps one and not the other — which it already did,
# reporting 0.1.0.dev0 from a 0.1.0 wheel.
__version__ = _metadata.version("proofstep-trajectory")

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
