"""Enumerations shared across the wire contract.

These are plain string enums rather than PostgreSQL enum types: adding a value to a
PG enum inside a transaction alongside other DDL is a recurring migration hazard, so
the database stores text with a CHECK constraint instead (docs/DATABASE_DESIGN.md §0).

Clients must tolerate unknown values. Deserializers degrade rather than raise — a
server that learns a new span type must not break older SDKs (docs/API_DESIGN.md §4).
"""

from __future__ import annotations

from enum import StrEnum


class SpanType(StrEnum):
    """What kind of operation a span represents."""

    AGENT = "agent"
    WORKFLOW = "workflow"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVER = "retriever"
    EMBEDDING = "embedding"
    GUARDRAIL = "guardrail"
    EVALUATOR = "evaluator"
    CUSTOM = "custom"

    @classmethod
    def _missing_(cls, value: object) -> SpanType:  # noqa: ARG003 — signature fixed by Enum
        """Degrade unknown span types to CUSTOM instead of raising."""
        return cls.CUSTOM


class Status(StrEnum):
    """Terminal status of a span, trace, or task execution."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    UNSET = "unset"


class Severity(StrEnum):
    """How a rule or gate failure should be treated."""

    BLOCK = "block"
    WARN = "warn"


class Verdict(StrEnum):
    """Outcome of a gate, a gate set, or a whole run.

    ERROR is distinct from FAIL on purpose: a metric that could not be computed is
    not the same as a metric that came out bad, and collapsing the two is how a
    broken evaluator silently reports success (docs/EVALUATION_ENGINE.md §1).
    """

    PASS = "pass"  # noqa: S105 — a verdict, not a credential
    WARN = "warn"
    FAIL = "fail"
    ERROR = "error"

    @property
    def is_blocking(self) -> bool:
        return self in (Verdict.FAIL, Verdict.ERROR)


class CaptureMode(StrEnum):
    """How much payload data is retained.

    Ordered most- to least-permissive; `resolve()` picks the most restrictive of
    several settings, which is how project, environment, SDK, and per-span settings
    combine (docs/SECURITY.md §8).
    """

    FULL = "full"
    REDACTED = "redacted"
    METADATA_ONLY = "metadata_only"
    DISABLED = "disabled"

    @property
    def rank(self) -> int:
        return _CAPTURE_RANK[self]

    @property
    def stores_payloads(self) -> bool:
        return self in (CaptureMode.FULL, CaptureMode.REDACTED)

    @classmethod
    def resolve(cls, *modes: CaptureMode | None) -> CaptureMode:
        """Return the most restrictive of the given modes.

        Defaults to REDACTED when nothing is specified: the safe default is not
        collecting, because data that is never stored cannot leak.
        """
        present = [m for m in modes if m is not None]
        if not present:
            return cls.REDACTED
        return max(present, key=lambda m: m.rank)


_CAPTURE_RANK: dict[CaptureMode, int] = {
    CaptureMode.FULL: 0,
    CaptureMode.REDACTED: 1,
    CaptureMode.METADATA_ONLY: 2,
    CaptureMode.DISABLED: 3,
}


class OutputKind(StrEnum):
    """The shape of the value an evaluator produces."""

    BINARY = "binary"
    SCORE = "score"
    CATEGORICAL = "categorical"
    NUMERIC = "numeric"


class ResultStatus(StrEnum):
    """Outcome of running the task against a single example."""

    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class ExitCode:
    """Process exit codes for the CLI (docs/EVALUATION_ENGINE.md §7)."""

    PASS = 0
    BLOCKING_FAILURE = 1
    EXECUTION_ERROR = 2
    CONFIGURATION_ERROR = 3
    CANCELLED = 130
