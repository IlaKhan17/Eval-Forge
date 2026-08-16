"""Policy parsing with line-precise errors.

The source YAML is always retained alongside the parsed form. Error messages that
point at a line number are the difference between a policy people maintain and one
they abandon.
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from proofstep_trajectory.predicates import PredicateError, compile_predicate
from proofstep_trajectory.schema import RULE_KINDS, Policy


class PolicyError(ValueError):
    """A policy could not be parsed or is semantically invalid."""


@dataclass(frozen=True)
class LoadedPolicy:
    policy: Policy
    source: str
    path: str | None = None
    content_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    line_of_rule: dict[str, int] = field(default_factory=dict)

    def line_for(self, rule_id: str) -> int | None:
        return self.line_of_rule.get(rule_id)


def load_policy_file(path: str | Path) -> LoadedPolicy:
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read policy {path}: {exc}"
        raise PolicyError(msg) from exc
    return load_policy(source, path=str(path))


def load_policy(source: str, *, path: str | None = None) -> LoadedPolicy:
    """Parse, validate, and hash a policy."""
    where = path or "<policy>"

    try:
        raw = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"{where}:{mark.line + 1}:{mark.column + 1}" if mark else where
        problem = getattr(exc, "problem", str(exc))
        msg = f"{location}: invalid YAML: {problem}"
        raise PolicyError(msg) from exc

    if not isinstance(raw, dict):
        msg = f"{where}: a policy must be a YAML mapping, got {type(raw).__name__}"
        raise PolicyError(msg)

    lines = _rule_lines(source)
    _check_rule_kinds(raw, where, lines)

    try:
        policy = Policy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(_format_validation(exc, where, raw, lines)) from exc

    warnings = _validate_predicates(policy, where, lines)

    return LoadedPolicy(
        policy=policy,
        source=source,
        path=path,
        content_hash=hashlib.sha256(source.encode("utf-8")).hexdigest()[:16],
        warnings=warnings,
        line_of_rule=lines,
    )


def _rule_lines(source: str) -> dict[str, int]:
    """Map rule id -> 1-based line, by scanning for `id:` keys.

    A regex over the source rather than a custom YAML loader: this only needs to be
    good enough to point a human at the right place.
    """
    lines: dict[str, int] = {}
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith(("- id:", "id:")):
            continue
        value = stripped.split("id:", 1)[1].strip().strip("\"'")
        if value and value not in lines:
            lines[value] = number
    return lines


def _check_rule_kinds(raw: dict[str, Any], where: str, lines: dict[str, int]) -> None:
    """Report an unknown `kind` before pydantic's discriminated-union error.

    Pydantic's message for a bad discriminator lists every variant and is close to
    unreadable; this one names the offending kind and suggests the closest match.
    """
    rules = raw.get("rules")
    if not isinstance(rules, list):
        return
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            msg = f"{where}: rules[{index}] must be a mapping"
            raise PolicyError(msg)
        kind = rule.get("kind")
        if kind is None:
            msg = f"{where}: rules[{index}] has no `kind`. One of: {', '.join(sorted(RULE_KINDS))}"
            raise PolicyError(msg)
        if kind not in RULE_KINDS:
            rule_id = str(rule.get("id", f"rules[{index}]"))
            at = f":{lines[rule_id]}" if rule_id in lines else ""
            suggestion = _closest(str(kind), RULE_KINDS)
            hint = f" Did you mean {suggestion!r}?" if suggestion else ""
            msg = (
                f"{where}{at}: rule {rule_id!r} has unknown kind {kind!r}.{hint} "
                f"Valid kinds: {', '.join(sorted(RULE_KINDS))}"
            )
            raise PolicyError(msg)


def _format_validation(
    exc: ValidationError, where: str, raw: dict[str, Any], lines: dict[str, int]
) -> str:
    parts: list[str] = [f"{where}: policy is invalid:"]
    rules: list[Any] = raw["rules"] if isinstance(raw.get("rules"), list) else []
    for error in exc.errors():
        location = error["loc"]
        line = None
        if len(location) >= 2 and location[0] == "rules" and isinstance(location[1], int):
            index = location[1]
            if index < len(rules) and isinstance(rules[index], dict):
                rule_id = str(rules[index].get("id", ""))
                line = lines.get(rule_id)
        prefix = f"  {where}:{line}: " if line else "  "
        field_path = ".".join(str(p) for p in location if not isinstance(p, int))
        parts.append(f"{prefix}{field_path or '<root>'}: {error['msg']}")
    return "\n".join(parts)


def _validate_predicates(policy: Policy, where: str, lines: dict[str, int]) -> list[str]:
    """Compile every predicate at load time.

    A predicate that only fails when a particular trace arrives is a policy that
    silently stops protecting you.
    """
    for rule in policy.rules:
        for attribute in ("when", "require"):
            source = getattr(rule, attribute, None)
            if not source:
                continue
            try:
                compile_predicate(source)
            except PredicateError as exc:
                at = f":{lines[rule.id]}" if rule.id in lines else ""
                msg = f"{where}{at}: rule {rule.id!r}: {exc}"
                raise PolicyError(msg) from exc

            # A specific trap worth refusing rather than documenting: action names
            # contain dots, so `actions.gmail.send` parses as nested attribute access,
            # resolves to None, and produces a rule that silently never fires.
            if "actions." in source:
                at = f":{lines[rule.id]}" if rule.id in lines else ""
                msg = (
                    f"{where}{at}: rule {rule.id!r}: `actions.<name>` does not work — "
                    "action names contain dots, so this resolves to nothing and the "
                    "rule would never fire. Use membership instead, e.g. "
                    "\"'gmail.send' in actions\"."
                )
                raise PolicyError(msg)
    return []


def check_actions(loaded: LoadedPolicy, observed: set[str]) -> list[str]:
    """Warn about referenced actions that no trace has ever produced.

    A rule naming an action that never appears is silently vacuous, which is the
    main way a policy suite rots: it keeps passing while protecting nothing.
    """
    referenced = loaded.policy.referenced_actions()
    unknown = sorted(referenced - observed)
    if not unknown or not observed:
        return []
    known = ", ".join(sorted(observed)[:12])
    return [
        f"Policy {loaded.policy.name!r} references action(s) never seen in this trace: "
        f"{', '.join(unknown)}. Rules naming them cannot fire. Observed actions: {known}"
    ]


def _closest(value: str, candidates: frozenset[str]) -> str | None:
    matches = difflib.get_close_matches(value, sorted(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None
