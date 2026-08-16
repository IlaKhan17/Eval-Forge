"""The execution engine: run a dataset through a task, score it, aggregate, gate.

Design notes worth stating, each addressing a specific failure this avoids:

**Two semaphores, not one.** Task concurrency and judge concurrency are separate
limits because they contend for different resources — the user's application versus
the judge provider's rate limit. Coupling them means one throttles the other.

**Journaling.** Each result is appended to a JSONL journal the moment it completes,
so a crash at example 190/200 loses nothing and `resume_from` skips what finished.
This costs about twenty lines and removes the worst experience in eval tooling:
losing a forty-minute, twelve-dollar run to a transient 429.

**Retries are for transport, never for scores.** A low score is a result. Retrying
until it improves would be measuring the retry policy, not the system.

**Partial failure has a ceiling.** Above `max_error_rate` the run is FAILED, not
partial. A run where a third of examples crashed must not report a cheerful average
over the survivors.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from proofstep_core.aggregate import aggregate_scores
from proofstep_core.dataset import Dataset
from proofstep_core.gates import GateReport, evaluate_gates
from proofstep_core.significance import SignificanceResult, analyse_all
from proofstep_core.types import CorpusEvaluator, EvalContext, Evaluator, ModelClient
from proofstep_types import (
    CalibrationStatus,
    Example,
    ExampleResult,
    ExitCode,
    GateSet,
    Metric,
    ResultStatus,
    Score,
    TaskError,
    Trace,
)


@dataclass(slots=True)
class RunConfig:
    concurrency: int = 8
    judge_concurrency: int = 4
    timeout_s: float = 120.0
    evaluator_timeout_s: float = 60.0
    retries: int = 2
    retry_backoff_s: float = 0.5
    max_error_rate: float = 0.10
    slice_by: Sequence[str] = ()
    seed: int = 42
    journal_path: Path | None = None
    resume_from: Path | None = None
    max_cost: Decimal | None = None
    confidence_intervals: bool = True
    on_result: Callable[[ExampleResult], None] | None = None


@dataclass(slots=True)
class EvalResult:
    """Everything a run produced."""

    suite: str
    results: list[ExampleResult] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    gates: GateReport = field(default_factory=GateReport)
    dataset_name: str = ""
    dataset_version: str = ""
    dataset_hash: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    cancelled: bool = False
    aborted_reason: str | None = None
    #: Paired significance tests for the gated metrics, when a baseline's per-example results were
    #: available. Empty rather than absent when no test ran, so a reader can tell "not tested" from
    #: "tested and unremarkable" — the report renders the difference.
    significance: dict[str, SignificanceResult] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.ended_at - self.started_at

    @property
    def total_cost(self) -> Decimal:
        return sum((r.total_cost for r in self.results), Decimal(0))

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def exit_code(self) -> int:
        if self.cancelled:
            return ExitCode.CANCELLED
        if self.aborted_reason:
            return ExitCode.EXECUTION_ERROR
        return self.gates.exit_code

    def metric(self, key: str, **slice_: str) -> Metric | None:
        wanted = slice_ or None
        return next((m for m in self.metrics if m.key == key and m.slice == wanted), None)

    def failures(self) -> list[ExampleResult]:
        return [r for r in self.results if not r.ok]


async def run_suite(
    *,
    dataset: Dataset | Iterable[Example],
    task: Callable[[Example], Any],
    evaluators: Sequence[Evaluator] = (),
    corpus_evaluators: Sequence[CorpusEvaluator] = (),
    gate_set: GateSet | None = None,
    baseline: Sequence[Metric] | None = None,
    baseline_results: Sequence[ExampleResult] | None = None,
    models: ModelClient | None = None,
    config: RunConfig | None = None,
    suite_name: str = "eval",
    judge_metrics: Sequence[str] = (),
    calibrations: Mapping[str, CalibrationStatus] | None = None,
    dataset_match: bool = True,
) -> EvalResult:
    """Execute a full evaluation and return everything it produced."""
    cfg = config or RunConfig()
    data = dataset if isinstance(dataset, Dataset) else Dataset(dataset)

    completed = _load_journal(cfg.resume_from) if cfg.resume_from else {}
    pending = [e for e in data if e.id not in completed]

    result = EvalResult(
        suite=suite_name,
        dataset_name=data.name,
        dataset_version=data.version,
        dataset_hash=data.content_hash,
        started_at=time.monotonic(),
        results=list(completed.values()),
    )

    judge_gate = asyncio.Semaphore(cfg.judge_concurrency)
    budget = _Budget(cfg.max_cost)
    journal = _Journal(cfg.journal_path)

    # A fixed pool of workers pulling from a queue, rather than one task per
    # example. Three reasons, in order of importance:
    #   1. The budget check is meaningful. With N tasks created upfront, every task
    #      passes its check before any of them commits a spend, so the cap does
    #      nothing. A worker only checks when it is about to pull more work, by
    #      which time earlier spends have landed.
    #   2. Memory is bounded. A 10 000-example dataset does not create 10 000 Task
    #      objects that all sit blocked on a semaphore.
    #   3. Overspend is bounded and statable: at most `concurrency` examples are in
    #      flight when the cap trips.
    queue: asyncio.Queue[Example] = asyncio.Queue()
    for example in pending:
        queue.put_nowait(example)

    produced: list[ExampleResult] = []
    stop = asyncio.Event()

    async def worker() -> None:
        while not stop.is_set():
            try:
                example = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                budget.check()
            except _BudgetExceededError as exc:
                result.aborted_reason = str(exc)
                stop.set()
                return
            produced.append(
                await _run_one(
                    example,
                    task=task,
                    evaluators=evaluators,
                    cfg=cfg,
                    models=models,
                    judge_gate=judge_gate,
                    budget=budget,
                    journal=journal,
                    on_result=cfg.on_result,
                )
            )

    try:
        async with asyncio.TaskGroup() as group:
            for index in range(max(1, min(cfg.concurrency, len(pending) or 1))):
                group.create_task(worker(), name=f"worker-{index}")
    finally:
        # Cancellation propagates to the caller, as the asyncio contract requires.
        # Partial results are recoverable from the journal, which is exactly what it
        # is for — swallowing CancelledError to return a tidy object would make
        # `task.cancel()` a lie.
        journal.close()
        result.results.extend(produced)

    # Preserve dataset order regardless of completion order, so two runs of the same
    # suite produce byte-identical reports.
    order = {example.id: i for i, example in enumerate(data)}
    result.results.sort(key=lambda r: order.get(r.example_id, len(order)))

    result.metrics = aggregate_scores(
        result.results,
        slice_by=cfg.slice_by,
        confidence_intervals=cfg.confidence_intervals,
        seed=cfg.seed,
    )
    for corpus in corpus_evaluators:
        result.metrics.extend(corpus.evaluate_corpus(result.results))

    result.ended_at = time.monotonic()

    if gate_set is not None:
        # Paired tests only for the metrics a rule actually gates on. Testing every metric would
        # cost bootstrap resamples for numbers nobody is deciding anything with, and — worse —
        # inflate the multiple-comparison correction, making the gated metrics harder to call
        # significant because of metrics that were never in question.
        significance = None
        if baseline_results:
            gated = sorted({rule.metric_key for rule in gate_set.rules if rule.needs_significance})
            if gated:
                significance = analyse_all(result.results, list(baseline_results), gated)

        result.gates = evaluate_gates(
            gate_set,
            result.metrics,
            list(baseline) if baseline else None,
            dataset_match=dataset_match,
            judge_metrics=judge_metrics,
            calibrations=calibrations,
            significance=significance,
        )
        result.significance = significance or {}

    _check_error_ceiling(result, cfg)
    return result


def _check_error_ceiling(result: EvalResult, cfg: RunConfig) -> None:
    if not result.results or result.aborted_reason:
        return
    rate = result.error_count / len(result.results)
    if rate > cfg.max_error_rate:
        result.aborted_reason = (
            f"{result.error_count} of {len(result.results)} examples failed "
            f"({rate:.1%} > max_error_rate {cfg.max_error_rate:.1%}). The metrics "
            "below describe only the examples that survived and should not be "
            "compared against a healthy baseline."
        )


async def _run_one(
    example: Example,
    *,
    task: Callable[[Example], Any],
    evaluators: Sequence[Evaluator],
    cfg: RunConfig,
    models: ModelClient | None,
    judge_gate: asyncio.Semaphore,
    budget: _Budget,
    journal: _Journal,
    on_result: Callable[[ExampleResult], None] | None,
) -> ExampleResult:
    result = ExampleResult(
        example_id=example.id,
        expected=example.expected,
        metadata=dict(example.metadata),
    )

    started = time.monotonic()
    output, trace, error, status, retries = await _invoke(task, example, cfg)
    result.latency_ms = int((time.monotonic() - started) * 1000)

    result.output = output
    result.trace = trace
    result.error = error
    result.status = status
    result.retry_count = retries
    if trace is not None:
        result.cost = trace.total_cost
        result.tokens = trace.total_tokens

    if status is ResultStatus.OK:
        result.scores = await _score(
            example, result, evaluators, cfg=cfg, models=models, judge_gate=judge_gate
        )
        budget.spend(sum((s.cost for s in result.scores), Decimal(0)))

    journal.write(result)
    if on_result is not None:
        on_result(result)
    return result


async def _invoke(
    task: Callable[[Example], Any], example: Example, cfg: RunConfig
) -> tuple[Any, Trace | None, TaskError | None, ResultStatus, int]:
    """Call the task with timeout and transport retries."""
    last: BaseException | None = None
    for attempt in range(cfg.retries + 1):
        try:
            async with asyncio.timeout(cfg.timeout_s):
                raw = task(example)
                output = await raw if inspect.isawaitable(raw) else raw
            trace = output.trace if hasattr(output, "trace") else None
            if hasattr(output, "output"):
                output = output.output
            return output, trace, None, ResultStatus.OK, attempt
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            last = exc
            if attempt >= cfg.retries:
                return (
                    None,
                    None,
                    TaskError(type="TimeoutError", message=f"exceeded {cfg.timeout_s}s"),
                    ResultStatus.TIMEOUT,
                    attempt,
                )
        except Exception as exc:
            last = exc
            if not _is_retryable(exc) or attempt >= cfg.retries:
                return (
                    None,
                    None,
                    TaskError(type=type(exc).__name__, message=str(exc)),
                    ResultStatus.ERROR,
                    attempt,
                )
        await asyncio.sleep(cfg.retry_backoff_s * (2**attempt))

    return (
        None,
        None,
        TaskError(type=type(last).__name__ if last else "Unknown", message=str(last)),
        ResultStatus.ERROR,
        cfg.retries,
    )


_RETRYABLE = (TimeoutError, ConnectionError, OSError)


def _is_retryable(exc: BaseException) -> bool:
    """Only transport failures are retried.

    A ValueError from the task is a real result about the system under test; retrying
    it would hide a deterministic bug behind three identical failures.
    """
    return isinstance(exc, _RETRYABLE)


async def _score(
    example: Example,
    result: ExampleResult,
    evaluators: Sequence[Evaluator],
    *,
    cfg: RunConfig,
    models: ModelClient | None,
    judge_gate: asyncio.Semaphore,
) -> list[Score]:
    ctx = EvalContext(
        example=example,
        output=result.output,
        expected=example.expected,
        trace=result.trace,
        metadata=dict(example.metadata),
        models=models,
        seed=cfg.seed,
    )

    async def one(evaluator: Evaluator) -> list[Score]:
        gate = judge_gate if _is_judge(evaluator) else _NULL_GATE
        try:
            async with gate, asyncio.timeout(cfg.evaluator_timeout_s):
                produced = await evaluator.evaluate(ctx)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return [Score.failure(evaluator.name, f"evaluator exceeded {cfg.evaluator_timeout_s}s")]
        except Exception as exc:
            return [Score.failure(evaluator.name, f"{type(exc).__name__}: {exc}")]
        return produced if isinstance(produced, list) else [produced]

    batches = await asyncio.gather(*(one(e) for e in evaluators))
    return [score for batch in batches for score in batch]


def _is_judge(evaluator: Evaluator) -> bool:
    return getattr(evaluator, "uses_model", None) is True or type(evaluator).__name__ == "LLMJudge"


class _NullGate:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


_NULL_GATE = _NullGate()


class _BudgetExceededError(Exception):
    pass


class _Budget:
    """Hard ceiling on judge spend.

    A runaway suite is a denial-of-wallet against your own budget, and the only
    reliable defence is refusing to start further work once the cap is hit.
    """

    def __init__(self, limit: Decimal | None) -> None:
        self.limit = limit
        self.spent = Decimal(0)

    def spend(self, amount: Decimal) -> None:
        self.spent += amount

    def check(self) -> None:
        if self.limit is not None and self.spent >= self.limit:
            msg = (
                f"Cost budget exhausted: spent ${self.spent:.4f} of ${self.limit:.4f}. "
                "Remaining examples were not run."
            )
            raise _BudgetExceededError(msg)


class _Journal:
    """Append-only JSONL written as results complete."""

    def __init__(self, path: Path | None) -> None:
        self._handle = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8")

    def write(self, result: ExampleResult) -> None:
        if self._handle is None:
            return
        self._handle.write(result.model_dump_json(exclude={"trace"}) + "\n")
        # Flushed per result on purpose: an unflushed journal does not survive the
        # crash it exists to protect against.
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def _load_journal(path: Path) -> dict[str, ExampleResult]:
    if not path.exists():
        return {}
    completed: dict[str, ExampleResult] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                # A torn final line from a hard kill is expected, not exceptional.
                result = ExampleResult.model_validate_json(stripped)
                completed[result.example_id] = result
    return completed
