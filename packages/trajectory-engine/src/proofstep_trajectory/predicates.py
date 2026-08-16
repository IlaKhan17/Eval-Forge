"""A restricted predicate expression evaluator.

The one concession to expressiveness in an otherwise declarative schema (ADR-011).
Roughly 150 lines, not a language: comparisons, boolean operators, membership,
literals, dotted field access, and a handful of functions.

Explicitly not supported, and rejected at parse time rather than at run time:
function definitions, calls to anything not on the allow-list, attribute access
(`x.__class__`), subscripting beyond the resolved namespace, comprehensions, lambdas,
imports, assignment, and f-strings. The AST is walked with an explicit allow-list, so
a node type nobody thought about is a parse error rather than an execution path.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Any

MAX_LENGTH = 500


class PredicateError(ValueError):
    """A predicate could not be parsed or evaluated."""


_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Attribute,
    ast.Call,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.USub,
    ast.UAdd,
)

_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": len,
    "lower": lambda s: str(s).lower(),
    "upper": lambda s: str(s).upper(),
    "startswith": lambda s, p: str(s).startswith(str(p)),
    "endswith": lambda s, p: str(s).endswith(str(p)),
    "contains": lambda hay, needle: needle in hay if hay is not None else False,
    "exists": lambda v: v is not None,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "abs": abs,
    "min": min,
    "max": max,
}


def compile_predicate(source: str) -> ast.Expression:
    """Parse and validate a predicate. Raises `PredicateError` on anything unsafe."""
    if len(source) > MAX_LENGTH:
        msg = f"predicate exceeds {MAX_LENGTH} characters; simplify it or use a rule"
        raise PredicateError(msg)
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        msg = f"invalid predicate {source!r}: {exc.msg}"
        raise PredicateError(msg) from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            msg = (
                f"predicate {source!r} uses {type(node).__name__}, which is not allowed. "
                "Predicates support comparisons, and/or/not, in, field access, and a "
                "small set of functions."
            )
            raise PredicateError(msg)
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            msg = f"predicate {source!r}: attribute {node.attr!r} is not accessible"
            raise PredicateError(msg)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                msg = f"predicate {source!r}: only direct calls to allowed functions are permitted"
                raise PredicateError(msg)
            if node.func.id not in _FUNCTIONS:
                allowed = ", ".join(sorted(_FUNCTIONS))
                msg = f"predicate {source!r}: unknown function {node.func.id!r}. Allowed: {allowed}"
                raise PredicateError(msg)
    return tree


def evaluate(source: str, namespace: dict[str, Any], *, tree: ast.Expression | None = None) -> bool:
    """Evaluate a predicate against a namespace, returning a bool."""
    compiled = tree or compile_predicate(source)
    try:
        value = _eval(compiled.body, namespace)
    except PredicateError:
        raise
    except Exception as exc:
        msg = f"predicate {source!r} failed to evaluate: {type(exc).__name__}: {exc}"
        raise PredicateError(msg) from exc
    return bool(value)


def _eval(node: ast.AST, ns: dict[str, Any]) -> Any:  # noqa: PLR0911, PLR0912
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in ns:
            # Unknown identifiers resolve to None rather than raising: policies read
            # optional metadata, and `metadata.foo == 'x'` on a trace without `foo`
            # should be false, not an error.
            return None
        return ns[node.id]

    if isinstance(node, ast.Attribute):
        base = _eval(node.value, ns)
        if isinstance(base, dict):
            return base.get(node.attr)
        return getattr(base, node.attr, None)

    if isinstance(node, ast.BoolOp):
        values = node.values
        if isinstance(node.op, ast.And):
            return all(_truthy(_eval(v, ns)) for v in values)
        return any(_truthy(_eval(v, ns)) for v in values)

    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, ns)
        if isinstance(node.op, ast.Not):
            return not _truthy(operand)
        if isinstance(node.op, ast.USub):
            return -operand
        return +operand

    if isinstance(node, ast.Compare):
        return _compare(node, ns)

    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return [_eval(e, ns) for e in node.elts]

    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        args = [_eval(a, ns) for a in node.args]
        return _FUNCTIONS[node.func.id](*args)

    msg = f"unsupported expression node {type(node).__name__}"
    raise PredicateError(msg)


def _compare(node: ast.Compare, ns: dict[str, Any]) -> bool:
    left = _eval(node.left, ns)
    for op, comparator in zip(node.ops, node.comparators, strict=True):
        right = _eval(comparator, ns)
        if not _apply(op, left, right):
            return False
        left = right
    return True


def _apply(op: ast.cmpop, left: Any, right: Any) -> bool:  # noqa: PLR0911
    if isinstance(op, ast.Eq):
        return bool(left == right)
    if isinstance(op, ast.NotEq):
        return bool(left != right)
    if isinstance(op, ast.Is):
        return left is right
    if isinstance(op, ast.IsNot):
        return left is not right
    if isinstance(op, ast.In):
        return _contains(right, left)
    if isinstance(op, ast.NotIn):
        return not _contains(right, left)

    # Ordering against a missing field is false, not a crash. A policy reading
    # optional metadata must not break the whole evaluation when it is absent.
    if left is None or right is None:
        return False
    if isinstance(op, ast.Lt):
        return bool(left < right)
    if isinstance(op, ast.LtE):
        return bool(left <= right)
    if isinstance(op, ast.Gt):
        return bool(left > right)
    if isinstance(op, ast.GtE):
        return bool(left >= right)

    msg = f"unsupported comparison {type(op).__name__}"
    raise PredicateError(msg)


def _contains(container: Any, item: Any) -> bool:
    if container is None:
        return False
    try:
        return item in container
    except TypeError:
        return False


def _truthy(value: Any) -> bool:
    return bool(value)
