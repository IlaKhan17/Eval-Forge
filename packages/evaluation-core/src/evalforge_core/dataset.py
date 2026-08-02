"""Dataset loading and slicing."""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from evalforge_types import Example, content_hash


class Dataset(Sequence[Example]):
    """An ordered, immutable collection of examples.

    Materialized in memory rather than streamed. A 10 000-example dataset at ~2 KB
    each is ~20 MB, and streaming would complicate resumption, slicing, and content
    hashing for a saving nobody currently needs (docs/OPEN_QUESTIONS.md Q8).
    """

    def __init__(self, examples: Iterable[Example], *, name: str = "", version: str = "") -> None:
        self._examples: tuple[Example, ...] = tuple(examples)
        self.name = name
        self.version = version
        self._check_unique_ids()

    def _check_unique_ids(self) -> None:
        seen: set[str] = set()
        duplicates: list[str] = []
        for example in self._examples:
            if example.id in seen:
                duplicates.append(example.id)
            seen.add(example.id)
        if duplicates:
            shown = ", ".join(sorted(set(duplicates))[:5])
            msg = (
                f"Dataset {self.name or '<unnamed>'} has duplicate example ids: {shown}. "
                "Ids must be unique — comparison between experiments matches on them, "
                "and duplicates would silently pair unrelated results."
            )
            raise ValueError(msg)

    # -------------------------------------------------------------- Sequence

    def __len__(self) -> int:
        return len(self._examples)

    def __iter__(self) -> Iterator[Example]:
        return iter(self._examples)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return Dataset(self._examples[index], name=self.name, version=self.version)
        return self._examples[index]

    def __repr__(self) -> str:
        label = f"{self.name}@{self.version}" if self.version else self.name or "<unnamed>"
        return f"Dataset({label!r}, n={len(self)})"

    # -------------------------------------------------------------- loading

    @classmethod
    def from_jsonl(cls, path: str | Path, **kw: Any) -> Dataset:
        """Load newline-delimited JSON.

        Errors name the line number: a dataset of 5 000 rows with one malformed
        entry is otherwise miserable to debug.
        """
        path = Path(path)
        examples: list[Example] = []
        with path.open(encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("//"):
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    msg = f"{path}:{lineno}: invalid JSON: {exc.msg}"
                    raise ValueError(msg) from exc
                examples.append(cls._to_example(raw, source=f"{path}:{lineno}", index=lineno))
        return cls(examples, name=kw.pop("name", path.stem), **kw)

    @classmethod
    def from_csv(cls, path: str | Path, **kw: Any) -> Dataset:
        """Load a flat CSV using ``input.*`` / ``expected.*`` / ``metadata.*`` columns.

        Deliberately limited to one level. Guessing at nested CSV semantics produces
        data-quality bugs that surface much later as mysterious eval failures, so
        anything nested must use JSONL instead.
        """
        path = Path(path)
        examples: list[Example] = []
        with path.open(encoding="utf-8", newline="") as handle:
            for index, row in enumerate(csv.DictReader(handle), start=1):
                nested: dict[str, dict[str, Any]] = {
                    "input": {},
                    "expected": {},
                    "metadata": {},
                }
                flat: dict[str, Any] = {}
                for column, value in row.items():
                    if column is None:
                        continue
                    prefix, _, rest = column.partition(".")
                    if rest and prefix in nested:
                        nested[prefix][rest] = value
                    else:
                        flat[column] = value
                raw: dict[str, Any] = {
                    "id": flat.pop("id", None),
                    "input": nested["input"] or flat,
                    "expected": nested["expected"] or None,
                    "metadata": nested["metadata"],
                }
                examples.append(cls._to_example(raw, source=f"{path}:{index}", index=index))
        return cls(examples, name=kw.pop("name", path.stem), **kw)

    @classmethod
    def from_dicts(cls, rows: Iterable[dict[str, Any]], **kw: Any) -> Dataset:
        return cls(
            [
                cls._to_example(row, source=f"row {i}", index=i)
                for i, row in enumerate(rows, start=1)
            ],
            **kw,
        )

    @staticmethod
    def _to_example(raw: Any, *, source: str, index: int) -> Example:
        if not isinstance(raw, dict):
            msg = f"{source}: expected a JSON object, got {type(raw).__name__}"
            raise TypeError(msg)
        if "input" not in raw:
            msg = f"{source}: example is missing the required 'input' field"
            raise ValueError(msg)
        return Example(
            id=str(raw.get("id") or raw.get("external_id") or f"ex-{index:04d}"),
            input=raw["input"],
            expected=raw.get("expected"),
            metadata=raw.get("metadata") or {},
            source_trace_id=raw.get("source_trace_id"),
            source_span_id=raw.get("source_span_id"),
        )

    # -------------------------------------------------------------- writing

    def to_jsonl(self, path: str | Path) -> None:
        with Path(path).open("w", encoding="utf-8") as handle:
            for example in self._examples:
                handle.write(example.canonical_json() + "\n")

    # -------------------------------------------------------------- slicing

    def filter(self, predicate: Callable[[Example], bool]) -> Dataset:
        return Dataset(
            [e for e in self._examples if predicate(e)], name=self.name, version=self.version
        )

    def limit(self, n: int) -> Dataset:
        return Dataset(self._examples[:n], name=self.name, version=self.version)

    def by_id(self, example_id: str) -> Example | None:
        return next((e for e in self._examples if e.id == example_id), None)

    @property
    def ids(self) -> list[str]:
        return [e.id for e in self._examples]

    @property
    def content_hash(self) -> str:
        """Hash of the exact content, used to prove two runs saw identical data."""
        return content_hash(list(self._examples))
