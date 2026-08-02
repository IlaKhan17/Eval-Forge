"""Dotted field-path resolution against an evaluation context.

Suites address data with strings like ``output.email.body`` or ``input.evidence``.
Resolution is deliberately strict: a path that does not exist raises rather than
returning None, because a typo'd field silently scoring zero across every example is
indistinguishable from a genuine failure, and that is the more dangerous outcome.
"""

from __future__ import annotations

from typing import Any

_ROOTS = frozenset({"output", "input", "expected", "metadata", "state"})


class PathError(LookupError):
    """A field path did not resolve."""


def resolve(root: Any, path: str) -> Any:
    """Resolve a dotted path against a value.

    Supports mapping keys, object attributes, and integer list indices.
    """
    current = root
    walked: list[str] = []
    for part in path.split("."):
        if not part:
            msg = f"Empty segment in field path {path!r}"
            raise PathError(msg)
        walked.append(part)
        current = _step(current, part, path, walked)
    return current


def _step(current: Any, part: str, path: str, walked: list[str]) -> Any:
    where = ".".join(walked[:-1]) or "<root>"
    if isinstance(current, dict):
        if part not in current:
            available = ", ".join(sorted(map(str, current))[:8]) or "<empty>"
            msg = f"{path!r}: no key {part!r} at {where}. Available: {available}"
            raise PathError(msg)
        return current[part]
    if isinstance(current, list | tuple):
        if not part.lstrip("-").isdigit():
            msg = f"{path!r}: {where} is a list; {part!r} is not an index"
            raise PathError(msg)
        index = int(part)
        try:
            return current[index]
        except IndexError as exc:
            msg = f"{path!r}: index {index} out of range at {where} (len {len(current)})"
            raise PathError(msg) from exc
    if hasattr(current, part):
        return getattr(current, part)
    msg = f"{path!r}: cannot resolve {part!r} on {type(current).__name__} at {where}"
    raise PathError(msg)


def resolve_in_context(
    path: str,
    *,
    output: Any,
    input_: dict[str, Any] | None = None,
    expected: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> Any:
    """Resolve a path rooted at one of the known context namespaces.

    A bare path with no recognized root is treated as relative to ``output``, since
    that is what suites reference most and requiring ``output.`` everywhere is noise.
    """
    head, _, rest = path.partition(".")
    if head not in _ROOTS:
        return resolve(output, path)

    roots: dict[str, Any] = {
        "output": output,
        "input": input_ or {},
        "expected": expected or {},
        "metadata": metadata or {},
        "state": state or {},
    }
    base = roots[head]
    return resolve(base, rest) if rest else base


def try_resolve(root: Any, path: str, default: Any = None) -> Any:
    """Resolve, returning ``default`` when the path is absent.

    Use only where absence is genuinely meaningful — policy conditions reading
    optional metadata, for instance. Evaluators should prefer `resolve`.
    """
    try:
        return resolve(root, path)
    except PathError:
        return default
