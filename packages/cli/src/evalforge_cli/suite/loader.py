"""Suite loading: interpolation, composition, validation.

Everything here happens *before* a single model call. A suite of 500 examples across
six judges is real money, so a misconfiguration must fail in milliseconds rather
than after the spend. Errors carry the file and line so the fix is obvious.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from evalforge_cli.suite.schema import Suite

INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Never persisted to the server, whatever a suite says. A provider key that reaches
# our storage is a leak we caused.
SECRET_HINTS = ("key", "token", "secret", "password", "credential")


class SuiteError(ValueError):
    """A suite could not be loaded, or is semantically invalid."""


@dataclass
class LoadedSuite:
    suite: Suite
    path: Path
    raw: dict[str, Any]
    hints: list[str] = field(default_factory=list)
    resolved_secrets: set[str] = field(default_factory=set)

    @property
    def directory(self) -> Path:
        return self.path.parent

    def resolve_path(self, relative: str) -> Path:
        """Resolve a path referenced by the suite, relative to the suite file.

        Relative to the *suite*, not the working directory: a suite must behave the
        same whether it is run from the repo root or from its own folder.
        """
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else (self.directory / candidate).resolve()


def load_suite(
    path: str | Path, *, overrides: dict[str, str] | None = None, _depth: int = 0
) -> LoadedSuite:
    file_path = Path(path).resolve()
    if not file_path.exists():
        msg = f"suite file not found: {file_path}"
        raise SuiteError(msg)

    source = file_path.read_text(encoding="utf-8")
    raw = _parse_yaml(source, file_path)

    if (parent := raw.get("extends")) is not None:
        if _depth >= 1:
            # One level only. Deep config inheritance is a well-known trap: the
            # effective configuration becomes something you have to execute to know.
            msg = (
                f"{file_path}: `extends` may only be one level deep. "
                f"{parent!r} itself extends another file."
            )
            raise SuiteError(msg)
        base_path = (file_path.parent / parent).resolve()
        base = load_suite(base_path, _depth=_depth + 1)
        raw = _merge(base.raw, raw)
        raw.pop("extends", None)

    secrets: set[str] = set()
    raw = _interpolate(raw, file_path, secrets)

    if overrides:
        for dotted, value in overrides.items():
            _apply_override(raw, dotted, value, file_path)

    lines = _key_lines(source)
    try:
        suite = Suite.model_validate(raw)
    except ValidationError as exc:
        raise SuiteError(_format_validation(exc, file_path, lines)) from exc

    loaded = LoadedSuite(suite=suite, path=file_path, raw=raw, resolved_secrets=secrets)
    loaded.hints = _semantic_checks(loaded, lines)
    return loaded


# ---------------------------------------------------------------------- parsing


def _parse_yaml(source: str, path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{path}:{mark.line + 1}:{mark.column + 1}" if mark else str(path)
        problem = getattr(exc, "problem", str(exc))
        msg = f"{where}: invalid YAML: {problem}"
        raise SuiteError(msg) from exc

    if not isinstance(parsed, dict):
        msg = f"{path}: a suite must be a YAML mapping, got {type(parsed).__name__}"
        raise SuiteError(msg)
    return parsed


def _key_lines(source: str) -> dict[str, int]:
    """Map keys and `name:` values to 1-based line numbers.

    Good enough to point a human at the right place, which is all a line number
    needs to do.
    """
    lines: dict[str, int] = {}
    for number, text in enumerate(source.splitlines(), start=1):
        stripped = text.strip().lstrip("- ")
        if ":" not in stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        lines.setdefault(key, number)
        cleaned = value.strip().strip("\"'")
        if key == "name" and cleaned:
            lines.setdefault(f"name={cleaned}", number)
    return lines


def _merge(base: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge; lists replace rather than concatenate.

    Concatenating would make it impossible for a child suite to *remove* an
    inherited evaluator, and silently accumulating them is worse than a redeclare.
    """
    merged = dict(base)
    for key, value in child.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------- interpolation


def _interpolate(value: Any, path: Path, secrets: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: _interpolate(v, path, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, path, secrets) for v in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        resolved = os.environ.get(name)
        if resolved is None:
            if default is None:
                # A missing variable with no default is an error, not an empty
                # string: silently substituting "" produces a suite that runs and
                # measures the wrong thing.
                msg = (
                    f"{path}: environment variable {name!r} is not set and has no default. "
                    f"Use ${{{name}:-fallback}} to provide one."
                )
                raise SuiteError(msg)
            resolved = default
        if any(hint in name.lower() for hint in SECRET_HINTS):
            secrets.add(name)
        return resolved

    return INTERPOLATION.sub(replace, value)


def _apply_override(raw: dict[str, Any], dotted: str, value: str, path: Path) -> None:
    """Apply `--set a.b.c=value`, coercing to the existing value's type."""
    parts = dotted.split(".")
    cursor: Any = raw
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            msg = f"{path}: --set {dotted} does not match any field in the suite"
            raise SuiteError(msg)
        cursor = cursor[part]

    leaf = parts[-1]
    if not isinstance(cursor, dict) or leaf not in cursor:
        msg = f"{path}: --set {dotted} does not match any field in the suite"
        raise SuiteError(msg)
    cursor[leaf] = _coerce(cursor[leaf], value)


def _coerce(existing: Any, value: str) -> Any:
    if isinstance(existing, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(existing, int) and not isinstance(existing, bool):
        return int(value)
    if isinstance(existing, float):
        return float(value)
    return value


# ------------------------------------------------------------------ validation


def _format_validation(exc: ValidationError, path: Path, lines: dict[str, int]) -> str:
    parts = [f"{path}: suite is invalid:"]
    for error in exc.errors():
        location = [str(p) for p in error["loc"]]
        field_path = ".".join(p for p in location if not p.isdigit())
        line = lines.get(location[-1]) or lines.get(location[0]) if location else None
        prefix = f"  {path}:{line}: " if line else "  "
        parts.append(f"{prefix}{field_path or '<root>'}: {error['msg']}")
    return "\n".join(parts)


def _semantic_checks(loaded: LoadedSuite, lines: dict[str, int]) -> list[str]:
    """Checks the type system cannot express. Errors raise; advice is returned."""
    suite = loaded.suite
    path = loaded.path
    produced = _declared_metrics(suite)

    for metric_key, gate in suite.gates.items():
        root = metric_key.split("[")[0]
        if not any(root == p or root.startswith(f"{p}_") for p in produced):
            # An error, not a warning. A gate naming a metric nothing produces is the
            # single most dangerous configuration bug: CI goes green while measuring
            # nothing, and nobody ever looks again.
            line = lines.get(metric_key)
            at = f"{path}:{line}: " if line else f"{path}: "
            msg = (
                f"{at}gate on {metric_key!r} matches no evaluator. "
                f"Declared evaluators: {', '.join(sorted(produced)) or '<none>'}"
            )
            raise SuiteError(msg)
        if gate.max_regression is not None and suite.baseline.strategy == "none":
            msg = (
                f"{path}: gate on {metric_key!r} sets max_regression but the suite's baseline "
                "strategy is 'none', so there is nothing to regress against."
            )
            raise SuiteError(msg)

    for evaluator in suite.evaluators:
        for attribute in ("schema_path", "rubric_path", "policy"):
            reference = getattr(evaluator, attribute, None)
            if reference and not loaded.resolve_path(reference).exists():
                line = lines.get(f"name={evaluator.name}")
                at = f"{path}:{line}: " if line else f"{path}: "
                msg = (
                    f"{at}evaluator {evaluator.name!r} references {attribute}="
                    f"{reference!r}, which does not exist "
                    f"(resolved to {loaded.resolve_path(reference)})"
                )
                raise SuiteError(msg)

    _check_metric_collisions(suite, path)

    if suite.dataset.is_local:
        assert suite.dataset.path is not None
        if not loaded.resolve_path(suite.dataset.path).exists():
            msg = f"{path}: dataset path {suite.dataset.path!r} does not exist"
            raise SuiteError(msg)

    return _hints(suite)


CORPUS_SUFFIXES: dict[str, tuple[str, ...]] = {
    "classification": (
        "accuracy",
        "precision",
        "recall",
        "f1",
        "macro_f1",
        "macro_recall",
        "micro_f1",
        "weighted_f1",
        "confusion_matrix",
    ),
    "ranking": ("precision_at_k", "recall_at_k", "ndcg_at_k", "mrr", "map"),
    "calibration": ("ece", "brier"),
}


def _check_metric_collisions(suite: Suite, path: Path) -> None:
    """Refuse a suite where two evaluators would write the same metric key.

    A corpus evaluator named `intent` emits `intent_accuracy`. If another evaluator
    is *called* `intent_accuracy`, both write one key and the reported value depends
    on ordering — which is exactly the kind of invisible nondeterminism that makes a
    number untrustworthy.
    """
    owners: dict[str, str] = {}
    for evaluator in suite.evaluators:
        emitted = {evaluator.name}
        for suffix in CORPUS_SUFFIXES.get(evaluator.type, ()):
            emitted.add(f"{evaluator.name}_{suffix}")

        for key in emitted:
            if key in owners and owners[key] != evaluator.name:
                msg = (
                    f"{path}: evaluators {owners[key]!r} and {evaluator.name!r} both produce "
                    f"the metric {key!r}. Rename one — otherwise the reported value depends "
                    "on evaluation order."
                )
                raise SuiteError(msg)
            owners[key] = evaluator.name


def _declared_metrics(suite: Suite) -> set[str]:
    """Metric keys the suite's evaluators can emit.

    Corpus evaluators emit prefixed families (`classification_macro_f1`), so the
    prefix is registered and gate matching allows a suffix.
    """
    produced: set[str] = set()
    for evaluator in suite.evaluators:
        produced.add(evaluator.name)
        if evaluator.type in ("classification", "ranking", "calibration"):
            produced.add(evaluator.name)
        if evaluator.type == "operational":
            produced.update(
                {
                    "total_cost",
                    "cost_per_example",
                    "judge_cost",
                    "total_tokens",
                    "error_rate",
                    "timeout_rate",
                    "retry_count",
                    "mean_latency_ms",
                    *(f"p{q}_latency_ms" for q in evaluator.percentiles),
                }
            )
    return produced


def _hints(suite: Suite) -> list[str]:
    hints: list[str] = []

    if suite.judge_ratio > 0.6:
        judges = sum(1 for e in suite.evaluators if e.type == "llm_judge")
        hints.append(
            f"{judges}/{len(suite.evaluators)} evaluators are LLM judges. Schema validity, "
            "placeholders, length limits and tool ordering are all deterministic, free and "
            "exactly reproducible — a judge-heavy suite is usually a modelling mistake."
        )

    uncalibrated = [
        e.name
        for e in suite.evaluators
        if e.type == "llm_judge" and e.calibration is None and _is_gated(suite, e.name)
    ]
    if uncalibrated:
        hints.append(
            f"gating on uncalibrated judge(s): {', '.join(uncalibrated)}. An uncalibrated "
            "judge is an unvalidated measuring instrument, and blocking a merge on one "
            "means blocking engineers on a number nobody has checked."
        )

    if not any(g.blocking for g in suite.gates.values()) and suite.gates:
        hints.append(
            "no gate is blocking, so this suite can never fail CI. That may be "
            "deliberate while you calibrate thresholds."
        )

    return hints


def _is_gated(suite: Suite, name: str) -> bool:
    return any(key.split("[")[0] == name for key in suite.gates)
