"""Policy schema — structured YAML, deliberately not a DSL (ADR-011).

Twelve rule kinds cover every enumerated case in the reference suites. Adding a
thirteenth should require a case that cannot be composed from these.

Policies are reviewed in pull-request diffs by people who did not write them, which
is the main reason this is a schema and not an expression language: a schema
validates statically with line-precise errors, where a DSL fails at run time.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from proofstep_types import Severity, SpanType

OrderMode = Literal["subsequence", "contiguous"]


class RuleBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    severity: Severity = Severity.BLOCK
    message: str | None = Field(default=None, description="Overrides the generated message")
    when: str | None = Field(
        default=None, description="Restricted predicate; the rule is skipped when false"
    )
    ignore_failed: bool = Field(
        default=False,
        description=(
            "Skip events whose span errored. Off by default: an *attempted* forbidden "
            "action is a violation even if the call failed — a gmail.send that 500s "
            "still tried to send."
        ),
    )


class RequiredOrder(RuleBase):
    kind: Literal["required_order"]
    steps: list[str] = Field(min_length=2)
    mode: OrderMode = "subsequence"


class RequiredAction(RuleBase):
    kind: Literal["required_action"]
    action: str
    min_count: int = 1


class ForbiddenAction(RuleBase):
    kind: Literal["forbidden_action"]
    actions: list[str] = Field(min_length=1)


class ForbiddenBefore(RuleBase):
    kind: Literal["forbidden_before"]
    action: str
    before: str


class ForbiddenAfter(RuleBase):
    kind: Literal["forbidden_after"]
    action: str
    after: str


class Limit(RuleBase):
    kind: Literal["limit"]
    action: str
    max_calls: int | None = None
    min_calls: int | None = None

    @model_validator(mode="after")
    def _needs_a_bound(self) -> Limit:
        if self.max_calls is None and self.min_calls is None:
            msg = f"limit rule {self.id!r} sets neither max_calls nor min_calls"
            raise ValueError(msg)
        return self


class UniqueAction(RuleBase):
    kind: Literal["unique_action"]
    action: str
    key: list[str] = Field(
        default_factory=lambda: ["action", "args_hash"],
        description="Composite identity; two events sharing it are a duplicate",
    )


class NoLoop(RuleBase):
    kind: Literal["no_loop"]
    window: int = 6
    min_repeats: int = 3
    key: list[str] = Field(default_factory=lambda: ["action", "args_hash"])

    @model_validator(mode="after")
    def _window_fits(self) -> NoLoop:
        if self.min_repeats > self.window:
            msg = (
                f"no_loop rule {self.id!r}: min_repeats ({self.min_repeats}) exceeds "
                f"window ({self.window}), so it can never fire"
            )
            raise ValueError(msg)
        return self


class ArgumentCondition(RuleBase):
    kind: Literal["argument_condition"]
    action: str
    require: str = Field(description="Restricted predicate evaluated per occurrence")


class Conditional(RuleBase):
    kind: Literal["conditional"]
    require_actions: list[str] = Field(default_factory=list)
    forbid_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_a_consequence(self) -> Conditional:
        if not self.require_actions and not self.forbid_actions:
            msg = (
                f"conditional rule {self.id!r} declares neither require_actions nor forbid_actions"
            )
            raise ValueError(msg)
        if self.when is None:
            msg = f"conditional rule {self.id!r} has no `when` predicate"
            raise ValueError(msg)
        return self


class FinalState(RuleBase):
    kind: Literal["final_state"]
    require: str


class MaxRetries(RuleBase):
    kind: Literal["max_retries"]
    action: str = "*"
    max_retries: int


Rule = Annotated[
    RequiredOrder
    | RequiredAction
    | ForbiddenAction
    | ForbiddenBefore
    | ForbiddenAfter
    | Limit
    | UniqueAction
    | NoLoop
    | ArgumentCondition
    | Conditional
    | FinalState
    | MaxRetries,
    Field(discriminator="kind"),
]

RULE_KINDS = frozenset(
    {
        "required_order",
        "required_action",
        "forbidden_action",
        "forbidden_before",
        "forbidden_after",
        "limit",
        "unique_action",
        "no_loop",
        "argument_condition",
        "conditional",
        "final_state",
        "max_retries",
    }
)


class Include(BaseModel):
    """Which spans become trajectory events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # llm/retriever/embedding spans are excluded by default: including them would
    # drown every policy in model calls.
    span_types: list[SpanType] = Field(
        default_factory=lambda: [SpanType.TOOL, SpanType.AGENT, SpanType.GUARDRAIL]
    )
    exclude_names: list[str] = Field(
        default_factory=list, description="Glob patterns matched against the action name"
    )


class Policy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    apiVersion: str = "proofstep.dev/v1"  # noqa: N815 — YAML field name, matches k8s convention
    kind: Literal["TrajectoryPolicy"] = "TrajectoryPolicy"
    name: str
    description: str | None = None
    aliases: dict[str, list[str]] = Field(
        default_factory=dict, description="canonical name -> raw names seen in traces"
    )
    include: Include = Field(default_factory=Include)
    rules: list[Rule] = Field(min_length=1)

    @field_validator("rules")
    @classmethod
    def _unique_rule_ids(cls, rules: list[Any]) -> list[Any]:
        seen: set[str] = set()
        for rule in rules:
            if rule.id in seen:
                msg = f"duplicate rule id {rule.id!r}; ids must be unique within a policy"
                raise ValueError(msg)
            seen.add(rule.id)
        return rules

    @model_validator(mode="after")
    def _aliases_are_unambiguous(self) -> Policy:
        """One raw name may not map to two canonical names.

        Silently picking one would make the policy's behaviour depend on dict
        ordering, which is exactly the kind of invisible nondeterminism that makes a
        verdict untrustworthy.
        """
        owner: dict[str, str] = {}
        for canonical, raws in self.aliases.items():
            for raw in raws:
                if raw in owner and owner[raw] != canonical:
                    msg = (
                        f"alias {raw!r} maps to both {owner[raw]!r} and {canonical!r}; "
                        "alias resolution must be unambiguous"
                    )
                    raise ValueError(msg)
                if raw == canonical:
                    continue
                owner[raw] = canonical

        # A canonical name appearing as someone else's alias would need two passes to
        # resolve, and two passes invite cycles.
        for canonical in self.aliases:
            if canonical in owner:
                msg = (
                    f"{canonical!r} is both a canonical name and an alias of "
                    f"{owner[canonical]!r}; alias chains are not supported"
                )
                raise ValueError(msg)
        return self

    def referenced_actions(self) -> set[str]:
        """Every action name the policy mentions, for unknown-action warnings."""
        names: set[str] = set()
        for rule in self.rules:
            for attr in ("action", "before", "after"):
                value = getattr(rule, attr, None)
                if isinstance(value, str) and value != "*":
                    names.add(value)
            for attr in ("actions", "steps", "require_actions", "forbid_actions"):
                values = getattr(rule, attr, None)
                if isinstance(values, list):
                    names.update(v for v in values if isinstance(v, str))
        return names
