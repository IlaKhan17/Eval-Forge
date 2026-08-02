"""The public Python API: `evaluate()` and `EvalSuite`.

Design goal: a working evaluation in about ten lines, with the full context still
reachable when you need it. Evaluator functions are adapted by *parameter name*, so
the common case stays one line while `(ctx)` remains available for evaluators that
need the trace (docs/SDK_AND_CLI.md §5).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from evalforge_core.dataset import Dataset
from evalforge_core.runner import EvalResult, RunConfig, run_suite
from evalforge_core.types import CorpusEvaluator, EvalContext, Evaluator, ModelClient
from evalforge_types import Example, GateRule, GateSet, Metric, Score, Severity


class FunctionEvaluator:
    """Wraps a plain function as an `Evaluator`, dispatching on parameter names.

    Accepted signatures: ``(output)``, ``(output, expected)``,
    ``(output, expected, example)``, ``(ctx)``, and any subset by name.
    """

    _KNOWN = frozenset({"output", "expected", "example", "ctx", "trace", "metadata", "input"})

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        version: int = 1,
        slice_when: Callable[[Example], bool] | None = None,
        slice_label: dict[str, str] | None = None,
    ) -> None:
        self.fn = fn
        self.name = name or fn.__name__
        self.version = version
        self.slice_when = slice_when
        self.slice_label = slice_label
        self._params = self._inspect(fn)
        self._is_async = inspect.iscoroutinefunction(fn)

    def _inspect(self, fn: Callable[..., Any]) -> list[str]:
        params = [
            p.name
            for p in inspect.signature(fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        if unknown := [p for p in params if p not in self._KNOWN]:
            msg = (
                f"Evaluator {self.name!r} has unrecognized parameter(s) {unknown}. "
                f"Parameters are matched by name; use any of: {sorted(self._KNOWN)}."
            )
            raise ValueError(msg)
        return params

    async def evaluate(self, ctx: EvalContext) -> Score | list[Score]:
        if self.slice_when is not None and not self.slice_when(ctx.example):
            # Out of slice: contribute nothing rather than a zero, which would drag
            # a sliced metric toward the behaviour of the whole population.
            return []

        available: dict[str, Any] = {
            "output": ctx.output,
            "expected": ctx.expected,
            "example": ctx.example,
            "ctx": ctx,
            "trace": ctx.trace,
            "metadata": ctx.metadata,
            "input": ctx.example.input,
        }
        kwargs = {name: available[name] for name in self._params}

        raw = self.fn(**kwargs)
        value = await raw if inspect.isawaitable(raw) else raw
        return self._coerce(value)

    def _coerce(self, value: Any) -> Score | list[Score]:
        if isinstance(value, Score):
            return value
        if isinstance(value, list):
            return [s if isinstance(s, Score) else self._coerce_one(s) for s in value]
        return self._coerce_one(value)

    def _coerce_one(self, value: Any) -> Score:  # noqa: PLR0911
        if isinstance(value, Score):
            return value
        if isinstance(value, bool):
            return Score.binary(self.name, value, slice=self.slice_label)
        if isinstance(value, int | float):
            return Score(metric=self.name, value=float(value), slice=self.slice_label)
        if isinstance(value, str):
            return Score(metric=self.name, label=value, slice=self.slice_label)
        if isinstance(value, dict):
            return Score(**{"metric": self.name, "slice": self.slice_label, **value})
        if value is None:
            return Score.failure(self.name, "evaluator returned None")
        return Score.failure(
            self.name, f"evaluator returned unsupported type {type(value).__name__}"
        )


async def evaluate(
    *,
    dataset: Dataset | Iterable[Example],
    task: Callable[[Example], Any],
    evaluators: Sequence[Evaluator | Callable[..., Any]] = (),
    corpus_evaluators: Sequence[CorpusEvaluator] = (),
    gates: Sequence[GateRule] | GateSet | None = None,
    baseline: Sequence[Metric] | None = None,
    models: ModelClient | None = None,
    name: str = "eval",
    **config: Any,
) -> EvalResult:
    """Run one evaluation. The imperative entry point."""
    return await run_suite(
        dataset=dataset,
        task=task,
        evaluators=[_adapt(e) for e in evaluators],
        corpus_evaluators=corpus_evaluators,
        gate_set=_as_gate_set(gates),
        baseline=baseline,
        models=models,
        config=RunConfig(**config) if config else None,
        suite_name=name,
    )


class EvalSuite:
    """Declarative suite built from decorators."""

    def __init__(
        self,
        name: str,
        *,
        dataset: Dataset | Iterable[Example],
        models: ModelClient | None = None,
        **config: Any,
    ) -> None:
        self.name = name
        self.dataset = dataset if isinstance(dataset, Dataset) else Dataset(dataset)
        self.models = models
        self.config = RunConfig(**config) if config else RunConfig()

        self._task: Callable[[Example], Any] | None = None
        self._evaluators: list[Evaluator] = []
        self._corpus: list[CorpusEvaluator] = []
        self._gates: list[GateRule] = []

    # ------------------------------------------------------------- decorators

    def task(self, fn: Callable[[Example], Any]) -> Callable[[Example], Any]:
        if self._task is not None:
            msg = f"Suite {self.name!r} already has a task ({self._task.__name__})"
            raise ValueError(msg)
        self._task = fn
        return fn

    def evaluator(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        version: int = 1,
        slice_when: Callable[[Example], bool] | None = None,
        slice_label: dict[str, str] | None = None,
    ) -> Any:
        def register(f: Callable[..., Any]) -> Callable[..., Any]:
            self._evaluators.append(
                FunctionEvaluator(
                    f, name=name, version=version, slice_when=slice_when, slice_label=slice_label
                )
            )
            return f

        return register(fn) if fn is not None else register

    def add(self, *evaluators: Evaluator | CorpusEvaluator) -> None:
        """Register built-in evaluator instances."""
        for evaluator in evaluators:
            if hasattr(evaluator, "evaluate_corpus"):
                self._corpus.append(evaluator)  # type: ignore[arg-type]
            else:
                self._evaluators.append(evaluator)

    def gate(
        self,
        metric_key: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        max_regression: float | None = None,
        max_relative_regression: float | None = None,
        blocking: bool = True,
        slice: dict[str, str] | None = None,
    ) -> None:
        self._gates.append(
            GateRule(
                metric_key=metric_key,
                minimum=minimum,
                maximum=maximum,
                max_absolute_regression=max_regression,
                max_relative_regression=max_relative_regression,
                severity=Severity.BLOCK if blocking else Severity.WARN,
                slice=slice,
            )
        )

    # ------------------------------------------------------------------- run

    def validate(self) -> list[str]:
        """Static problems worth catching before spending money on a run."""
        problems: list[str] = []
        if self._task is None:
            problems.append(f"Suite {self.name!r} has no @suite.task")
        if not self._evaluators and not self._corpus:
            problems.append(f"Suite {self.name!r} has no evaluators")
        if len(self.dataset) == 0:
            problems.append(f"Suite {self.name!r} has an empty dataset")

        # A gate naming a metric no evaluator can produce is the failure mode that
        # yields green CI while measuring nothing. Catch it before the run, not after.
        produced = {e.name for e in self._evaluators} | {c.name for c in self._corpus}
        for gate in self._gates:
            root = gate.metric_key.split(".")[0]
            if not any(root == p or gate.metric_key.startswith(p) for p in produced):
                problems.append(
                    f"Gate on {gate.metric_key!r} matches no evaluator. "
                    f"Declared evaluators: {', '.join(sorted(produced)) or '<none>'}"
                )

        judges = sum(1 for e in self._evaluators if type(e).__name__ == "LLMJudge")
        total = len(self._evaluators) + len(self._corpus)
        if total and judges / total > 0.6:
            problems.append(
                f"hint: {judges}/{total} evaluators are LLM judges. Schema validity, "
                "placeholders, length limits and tool ordering are all deterministic "
                "and free — a judge-heavy suite is usually a modelling mistake."
            )
        return problems

    async def run(self, *, baseline: Sequence[Metric] | None = None) -> EvalResult:
        problems = [p for p in self.validate() if not p.startswith("hint:")]
        if problems:
            msg = "Suite is not runnable:\n  - " + "\n  - ".join(problems)
            raise ValueError(msg)
        assert self._task is not None

        return await run_suite(
            dataset=self.dataset,
            task=self._task,
            evaluators=self._evaluators,
            corpus_evaluators=self._corpus,
            gate_set=GateSet(name=self.name, rules=self._gates) if self._gates else None,
            baseline=baseline,
            models=self.models,
            config=self.config,
            suite_name=self.name,
        )


def _adapt(evaluator: Evaluator | Callable[..., Any]) -> Evaluator:
    if isinstance(evaluator, Evaluator):
        return evaluator
    return FunctionEvaluator(evaluator)


def _as_gate_set(gates: Sequence[GateRule] | GateSet | None) -> GateSet | None:
    if gates is None:
        return None
    if isinstance(gates, GateSet):
        return gates
    return GateSet(rules=list(gates))
