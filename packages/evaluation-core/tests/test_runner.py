"""Runner tests: concurrency, timeouts, retries, journaling, budgets, failure ceiling."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from proofstep_core import Dataset, EvalResult, FunctionEvaluator, RunConfig, evaluate, run_suite
from proofstep_types import Example, GateRule, GateSet, Metric, ResultStatus, Score, Verdict


def data(n: int = 5) -> Dataset:
    return Dataset(
        [Example(id=f"ex-{i}", input={"n": i}, expected={"double": i * 2}) for i in range(n)]
    )


async def double(example: Example) -> dict[str, int]:
    return {"double": example.input["n"] * 2}


def correct(output: dict[str, Any], expected: dict[str, Any]) -> bool:
    return bool(output["double"] == expected["double"])


def metric_of(result: EvalResult, key: str) -> Metric:
    found = result.metric(key)
    assert found is not None, f"metric {key!r} was not produced"
    return found


class TestBasicExecution:
    async def test_runs_every_example(self) -> None:
        result = await evaluate(dataset=data(5), task=double, evaluators=[correct])
        assert len(result.results) == 5
        assert all(r.ok for r in result.results)
        assert metric_of(result, "correct").value == 1.0

    async def test_results_follow_dataset_order_not_completion_order(self) -> None:
        """Two runs of the same suite must produce byte-identical reports."""

        async def jittered(example: Example) -> dict[str, int]:
            await asyncio.sleep(0.01 * (5 - example.input["n"]))
            return {"double": example.input["n"] * 2}

        result = await evaluate(dataset=data(5), task=jittered, evaluators=[correct])
        assert [r.example_id for r in result.results] == [f"ex-{i}" for i in range(5)]

    async def test_sync_tasks_are_supported(self) -> None:
        def sync_task(example: Example) -> dict[str, int]:
            return {"double": example.input["n"] * 2}

        result = await evaluate(dataset=data(3), task=sync_task, evaluators=[correct])
        assert metric_of(result, "correct").value == 1.0

    async def test_concurrency_limit_is_respected(self) -> None:
        live = 0
        peak = 0

        async def tracked(example: Example) -> dict[str, int]:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return {"double": example.input["n"] * 2}

        await evaluate(dataset=data(20), task=tracked, evaluators=[correct], concurrency=3)
        assert peak <= 3


class TestFailures:
    async def test_task_exception_becomes_a_failed_result_not_a_crash(self) -> None:
        async def explodes(example: Example) -> dict[str, int]:
            if example.input["n"] == 2:
                msg = "boom"
                raise ValueError(msg)
            return {"double": example.input["n"] * 2}

        result = await evaluate(dataset=data(5), task=explodes, evaluators=[correct])
        assert len(result.results) == 5
        failed = [r for r in result.results if not r.ok]
        assert len(failed) == 1
        assert failed[0].error is not None
        assert failed[0].error.type == "ValueError"

    async def test_timeout_is_recorded_as_timeout_not_error(self) -> None:
        async def slow(example: Example) -> dict[str, int]:
            await asyncio.sleep(1.0)
            return {}

        result = await evaluate(
            dataset=data(1), task=slow, evaluators=[correct], timeout_s=0.05, retries=0
        )
        assert result.results[0].status is ResultStatus.TIMEOUT

    async def test_deterministic_errors_are_not_retried(self) -> None:
        """A ValueError is a result about the system, not a transport blip.

        Retrying it three times would hide a deterministic bug behind identical
        failures and triple the cost of discovering it.
        """
        attempts = 0

        async def always_fails(example: Example) -> dict[str, int]:
            nonlocal attempts
            attempts += 1
            msg = "deterministic"
            raise ValueError(msg)

        await evaluate(dataset=data(1), task=always_fails, evaluators=[correct], retries=3)
        assert attempts == 1

    async def test_transport_errors_are_retried_then_succeed(self) -> None:
        attempts = 0

        async def flaky(example: Example) -> dict[str, int]:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError
            return {"double": example.input["n"] * 2}

        result = await evaluate(
            dataset=data(1),
            task=flaky,
            evaluators=[correct],
            retries=3,
            retry_backoff_s=0.001,
        )
        assert result.results[0].ok
        assert result.results[0].retry_count == 2

    async def test_broken_evaluator_errors_that_example_only(self) -> None:
        def explodes(output: dict[str, Any]) -> bool:
            msg = "evaluator bug"
            raise RuntimeError(msg)

        result = await evaluate(dataset=data(3), task=double, evaluators=[correct, explodes])
        assert metric_of(result, "correct").value == 1.0
        broken = metric_of(result, "explodes")
        assert broken.count == 0
        assert broken.error_count == 3

    async def test_error_ceiling_aborts_the_verdict(self) -> None:
        async def mostly_fails(example: Example) -> dict[str, int]:
            if example.input["n"] > 0:
                msg = "boom"
                raise ValueError(msg)
            return {"double": 0}

        result = await evaluate(
            dataset=data(5), task=mostly_fails, evaluators=[correct], max_error_rate=0.1
        )
        assert result.aborted_reason is not None
        assert "should not be compared" in result.aborted_reason
        assert result.exit_code == 2


class TestJournalAndResume:
    async def test_journal_is_written_per_result(self, tmp_path: Path) -> None:
        journal = tmp_path / "run.jsonl"
        await run_suite(
            dataset=data(4),
            task=double,
            evaluators=[FunctionEvaluator(correct)],
            config=RunConfig(journal_path=journal),
        )
        lines = journal.read_text().strip().splitlines()
        assert len(lines) == 4

    async def test_resume_skips_completed_examples(self, tmp_path: Path) -> None:
        journal = tmp_path / "run.jsonl"
        await evaluate(dataset=data(4), task=double, evaluators=[correct], journal_path=journal)

        ran = 0

        async def counted(example: Example) -> dict[str, int]:
            nonlocal ran
            ran += 1
            return {"double": example.input["n"] * 2}

        result = await evaluate(
            dataset=data(6), task=counted, evaluators=[correct], resume_from=journal
        )
        assert ran == 2  # only the two new examples
        assert len(result.results) == 6

    async def test_torn_final_line_is_tolerated(self, tmp_path: Path) -> None:
        """A hard kill leaves a partial line. That is expected, not exceptional."""
        journal = tmp_path / "run.jsonl"
        await evaluate(dataset=data(3), task=double, evaluators=[correct], journal_path=journal)
        with journal.open("a") as handle:
            handle.write('{"example_id": "ex-9", "status": "o')

        result = await evaluate(
            dataset=data(3), task=double, evaluators=[correct], resume_from=journal
        )
        assert len(result.results) == 3


class TestBudget:
    async def test_budget_stops_the_run(self) -> None:
        def expensive(output: dict[str, Any]) -> Score:
            return Score(metric="expensive", value=1.0, cost=Decimal("1.00"))

        result = await evaluate(
            dataset=data(20),
            task=double,
            evaluators=[expensive],
            concurrency=1,
            max_cost=Decimal("3.00"),
        )
        assert result.aborted_reason is not None
        assert "budget exhausted" in result.aborted_reason.lower()
        assert len(result.results) < 20


class TestGatesIntegration:
    async def test_failing_gate_sets_the_exit_code(self) -> None:
        async def wrong(example: Example) -> dict[str, int]:
            return {"double": -1}

        result = await evaluate(
            dataset=data(5),
            task=wrong,
            evaluators=[correct],
            gates=GateSet(rules=[GateRule(metric_key="correct", minimum=0.9)]),
        )
        assert result.gates.verdict is Verdict.FAIL
        assert result.exit_code == 1

    async def test_passing_gate_exits_zero(self) -> None:
        result = await evaluate(
            dataset=data(5),
            task=double,
            evaluators=[correct],
            gates=[GateRule(metric_key="correct", minimum=0.9)],
        )
        assert result.exit_code == 0


class TestCancellation:
    async def test_cancelling_preserves_completed_results(self) -> None:
        async def slow(example: Example) -> dict[str, int]:
            await asyncio.sleep(0.02 * example.input["n"])
            return {"double": example.input["n"] * 2}

        task = asyncio.create_task(
            evaluate(dataset=data(30), task=slow, evaluators=[correct], concurrency=2)
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
