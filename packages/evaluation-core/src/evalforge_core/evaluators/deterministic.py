"""Deterministic evaluators — free, instant, and 100% reliable.

These run on every example and always run first. Most of what teams reach for an LLM
judge to check is mechanically checkable, and every metric implemented here is one
that a judge would do more slowly, more expensively, and less accurately
(docs/EVALUATION_ENGINE.md §2.1).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from typing import Any, Literal

from evalforge_core.paths import PathError, resolve, resolve_in_context
from evalforge_core.types import EvalContext, EvaluatorBase
from evalforge_types import Score

Normalization = Literal["none", "case", "whitespace", "punctuation", "all"]


class _FieldEvaluator(EvaluatorBase):
    """Shared plumbing for evaluators that read one field from the context."""

    def __init__(self, *, field: str = "output", **kw: Any) -> None:
        super().__init__(**kw)
        self.field = field

    def _read(self, ctx: EvalContext) -> tuple[Any, Score | None]:
        try:
            value = resolve_in_context(
                self.field,
                output=ctx.output,
                input_=ctx.example.input,
                expected=ctx.expected,
                metadata=ctx.metadata,
                state=ctx.trace.state if ctx.trace else None,
            )
        except PathError as exc:
            # A missing field is an error, not a zero: it means the suite is
            # misconfigured or the task changed shape, and reporting it as a bad
            # score would hide a broken suite behind a plausible-looking metric.
            return None, Score.failure(self.name, str(exc))
        return value, None


def _resolve_expected(path: str, expected: dict[str, Any]) -> Any:
    """Resolve a path that names a field of `expected`.

    A bare name like `intent` means `expected.intent`, not `output.intent`. Routing
    it through the generic resolver treated it as output-relative and errored on
    every example — a broken evaluator reported as a metric of zero measurements
    rather than as a mistake.
    """
    if path.startswith("expected."):
        return resolve(expected, path.removeprefix("expected."))
    return resolve(expected, path)


def _normalize(text: str, mode: Normalization) -> str:
    if mode == "none":
        return text
    result = text
    if mode in ("case", "all"):
        result = result.casefold()
    if mode in ("whitespace", "all"):
        result = " ".join(result.split())
    if mode in ("punctuation", "all"):
        result = "".join(ch for ch in result if not unicodedata.category(ch).startswith("P"))
        result = " ".join(result.split())
    return result


class ExactMatch(_FieldEvaluator):
    """Compare a field against the expected value."""

    requires_expected = True

    def __init__(
        self,
        *,
        field: str = "output",
        expected_field: str | None = None,
        normalize: Normalization = "none",
        name: str = "exact_match",
        **kw: Any,
    ) -> None:
        super().__init__(field=field, name=name, **kw)
        self.expected_field = expected_field
        self.normalize = normalize

    async def evaluate(self, ctx: EvalContext) -> Score:
        if failure := self._precondition_failure(ctx):
            return failure
        actual, error = self._read(ctx)
        if error:
            return error

        assert ctx.expected is not None
        if self.expected_field:
            try:
                wanted = _resolve_expected(self.expected_field, ctx.expected)
            except PathError as exc:
                return Score.failure(self.name, str(exc))
        else:
            tail = self.field.split(".")[-1]
            wanted = ctx.expected.get(tail, ctx.expected)

        if isinstance(actual, str) and isinstance(wanted, str):
            matched = _normalize(actual, self.normalize) == _normalize(wanted, self.normalize)
        else:
            matched = actual == wanted
        return Score.binary(self.name, matched, raw={"actual": actual, "expected": wanted})


class JsonSchemaMatch(_FieldEvaluator):
    """Validate a field against a JSON Schema.

    Implements the subset of draft 2020-12 that suites actually use, so the pure
    core stays dependency-free. Unknown keywords are ignored rather than rejected.
    """

    def __init__(
        self,
        schema: dict[str, Any],
        *,
        field: str = "output",
        name: str = "valid_schema",
        **kw: Any,
    ) -> None:
        super().__init__(field=field, name=name, **kw)
        self.schema = schema

    async def evaluate(self, ctx: EvalContext) -> Score:
        value, error = self._read(ctx)
        if error:
            return error
        errors = _validate(value, self.schema, "$")
        return Score.binary(self.name, not errors, raw={"errors": errors[:20]})


_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": (list, tuple),
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _validate(value: Any, schema: dict[str, Any], path: str) -> list[str]:  # noqa: PLR0912
    errors: list[str] = []

    if (expected := schema.get("type")) is not None:
        wanted = _TYPES.get(expected)
        # bool is a subclass of int in Python; JSON Schema treats them as distinct.
        ok = isinstance(value, wanted) if wanted else True
        if expected in ("number", "integer") and isinstance(value, bool):
            ok = False
        if not ok:
            errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
            return errors

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required property missing")
        properties = schema.get("properties", {})
        for key, subschema in properties.items():
            if key in value:
                errors.extend(_validate(value[key], subschema, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: additional property not allowed")

    if isinstance(value, list | tuple):
        if (items := schema.get("items")) is not None:
            for i, item in enumerate(value):
                errors.extend(_validate(item, items, f"{path}[{i}]"))
        if (n := schema.get("minItems")) is not None and len(value) < n:
            errors.append(f"{path}: expected at least {n} items, got {len(value)}")
        if (n := schema.get("maxItems")) is not None and len(value) > n:
            errors.append(f"{path}: expected at most {n} items, got {len(value)}")

    if isinstance(value, str):
        if (n := schema.get("minLength")) is not None and len(value) < n:
            errors.append(f"{path}: shorter than minLength {n}")
        if (n := schema.get("maxLength")) is not None and len(value) > n:
            errors.append(f"{path}: longer than maxLength {n}")
        if (pattern := schema.get("pattern")) is not None and not re.search(pattern, value):
            errors.append(f"{path}: does not match pattern {pattern!r}")

    if isinstance(value, int | float) and not isinstance(value, bool):
        if (n := schema.get("minimum")) is not None and value < n:
            errors.append(f"{path}: {value} below minimum {n}")
        if (n := schema.get("maximum")) is not None and value > n:
            errors.append(f"{path}: {value} above maximum {n}")

    if (allowed := schema.get("enum")) is not None and value not in allowed:
        errors.append(f"{path}: {value!r} not in enum {allowed}")

    return errors


class RegexMatch(_FieldEvaluator):
    """Assert a field matches every ``allow`` pattern and no ``deny`` pattern.

    Placeholder leakage — ``[Your Name]`` reaching a customer — is the archetypal
    case: embarrassing in production, and a regex catches it for free.
    """

    def __init__(
        self,
        *,
        allow: Sequence[str] = (),
        deny: Sequence[str] = (),
        field: str = "output",
        flags: int = 0,
        name: str = "regex",
        **kw: Any,
    ) -> None:
        super().__init__(field=field, name=name, **kw)
        self.allow = [re.compile(p, flags) for p in allow]
        self.deny = [re.compile(p, flags) for p in deny]

    async def evaluate(self, ctx: EvalContext) -> Score:
        value, error = self._read(ctx)
        if error:
            return error
        text = value if isinstance(value, str) else json.dumps(value, default=str)

        violations = [p.pattern for p in self.deny if p.search(text)]
        missing = [p.pattern for p in self.allow if not p.search(text)]
        passed = not violations and not missing
        detail: dict[str, Any] = {}
        if violations:
            detail["matched_denied"] = violations
        if missing:
            detail["missing_required"] = missing
        return Score.binary(self.name, passed, raw=detail or None)


class Contains(_FieldEvaluator):
    def __init__(
        self,
        substrings: Sequence[str],
        *,
        mode: Literal["all", "any"] = "all",
        case_sensitive: bool = True,
        field: str = "output",
        name: str = "contains",
        **kw: Any,
    ) -> None:
        super().__init__(field=field, name=name, **kw)
        self.substrings = list(substrings)
        self.mode = mode
        self.case_sensitive = case_sensitive

    async def evaluate(self, ctx: EvalContext) -> Score:
        value, error = self._read(ctx)
        if error:
            return error
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if not self.case_sensitive:
            text = text.casefold()
        needles = [s if self.case_sensitive else s.casefold() for s in self.substrings]
        found = [s for s in needles if s in text]
        passed = len(found) == len(needles) if self.mode == "all" else bool(found)
        missing = [s for s in needles if s not in text]
        return Score.binary(self.name, passed, raw={"found": found, "missing": missing})


class LengthWithin(_FieldEvaluator):
    def __init__(
        self,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        unit: Literal["chars", "words"] = "chars",
        field: str = "output",
        name: str = "length",
        **kw: Any,
    ) -> None:
        super().__init__(field=field, name=name, **kw)
        if minimum is None and maximum is None:
            msg = "LengthWithin needs at least one of minimum or maximum"
            raise ValueError(msg)
        self.minimum = minimum
        self.maximum = maximum
        self.unit = unit

    async def evaluate(self, ctx: EvalContext) -> Score:
        value, error = self._read(ctx)
        if error:
            return error
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        actual = len(text.split()) if self.unit == "words" else len(text)
        passed = (self.minimum is None or actual >= self.minimum) and (
            self.maximum is None or actual <= self.maximum
        )
        return Score.binary(
            self.name,
            passed,
            raw={"actual": actual, "unit": self.unit, "min": self.minimum, "max": self.maximum},
        )


class NumericRange(_FieldEvaluator):
    def __init__(
        self,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        inclusive: bool = True,
        field: str = "output",
        name: str = "numeric_range",
        **kw: Any,
    ) -> None:
        super().__init__(field=field, name=name, **kw)
        self.minimum = minimum
        self.maximum = maximum
        self.inclusive = inclusive

    async def evaluate(self, ctx: EvalContext) -> Score:
        value, error = self._read(ctx)
        if error:
            return error
        if isinstance(value, bool) or not isinstance(value, int | float):
            return Score.failure(
                self.name, f"{self.field} is {type(value).__name__}, expected a number"
            )
        lo_ok = self.minimum is None or (
            value >= self.minimum if self.inclusive else value > self.minimum
        )
        hi_ok = self.maximum is None or (
            value <= self.maximum if self.inclusive else value < self.maximum
        )
        return Score.binary(self.name, lo_ok and hi_ok, raw={"actual": value})


class SetComparison(_FieldEvaluator):
    """Compare a collection field against the expected collection."""

    requires_expected = True

    def __init__(
        self,
        *,
        mode: Literal["equals", "subset", "superset", "jaccard"] = "equals",
        field: str = "output",
        expected_field: str | None = None,
        name: str = "set_comparison",
        **kw: Any,
    ) -> None:
        super().__init__(field=field, name=name, **kw)
        self.mode = mode
        self.expected_field = expected_field

    async def evaluate(self, ctx: EvalContext) -> Score:
        if failure := self._precondition_failure(ctx):
            return failure
        value, error = self._read(ctx)
        if error:
            return error
        assert ctx.expected is not None
        tail = (self.expected_field or self.field).split(".")[-1]
        wanted_raw = ctx.expected.get(tail)
        if wanted_raw is None:
            return Score.failure(self.name, f"expected has no field {tail!r}")

        try:
            actual = {_hashable(v) for v in value}
            wanted = {_hashable(v) for v in wanted_raw}
        except TypeError as exc:
            return Score.failure(self.name, f"values are not comparable as a set: {exc}")

        detail = {
            "missing": sorted(map(str, wanted - actual))[:20],
            "unexpected": sorted(map(str, actual - wanted))[:20],
        }
        if self.mode == "jaccard":
            union = actual | wanted
            ratio = len(actual & wanted) / len(union) if union else 1.0
            return Score(metric=self.name, value=ratio, passed=ratio == 1.0, raw=detail)

        passed = {
            "equals": actual == wanted,
            "subset": actual <= wanted,
            "superset": actual >= wanted,
        }[self.mode]
        return Score.binary(self.name, passed, raw=detail)


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    return value
