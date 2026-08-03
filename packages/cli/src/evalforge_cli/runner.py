"""Orchestrate a suite: load, resolve, execute, aggregate, gate, report.

`--local` is the default path and needs no server, no account, and no network. That
is deliberate (ADR-017): the tool has to be useful before anyone signs up, and it
makes the whole pipeline testable without infrastructure.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from evalforge_cli.calibration_store import (
    StoredCalibration,
    evaluator_version_hash,
    load_all,
    status_for,
)
from evalforge_cli.registry import build_evaluators, estimate_judge_calls, load_rubric_text
from evalforge_cli.suite.loader import LoadedSuite, SuiteError
from evalforge_core import Dataset, EvalResult, RunConfig, run_suite
from evalforge_core.calibration import CalibrationRequirement, check_requirement
from evalforge_core.calibration_runner import report_from_dict
from evalforge_core.compare import compare_metrics
from evalforge_types import (
    CalibrationRequirementSpec,
    CalibrationStatus,
    ExitCode,
    GateRule,
    GateSet,
    Metric,
    Severity,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from evalforge_core.compare import Comparison


class RunError(RuntimeError):
    """The run could not be set up. Distinct from a run that produced a failure."""


@dataclass
class Plan:
    """What a run *would* do, for `--dry-run`."""

    suite: str
    dataset: str
    example_count: int
    evaluator_names: list[str] = field(default_factory=list)
    corpus_names: list[str] = field(default_factory=list)
    gate_count: int = 0
    judge_calls: int = 0
    baseline: str | None = None
    hints: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    result: EvalResult
    comparison: Comparison | None = None
    baseline_label: str | None = None

    @property
    def exit_code(self) -> int:
        return self.result.exit_code


def git_context() -> tuple[str | None, str | None, bool]:
    """Best-effort commit, branch, and dirty flag.

    Recorded because an experiment nobody can tie to a commit is an anecdote. Failure
    is tolerated: plenty of valid environments have no git.
    """

    def run(args: list[str]) -> str | None:
        try:
            output = subprocess.run(  # noqa: S603 — fixed argv, no shell
                ["git", *args],  # noqa: S607 — git is intentionally taken from PATH
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return output.stdout.strip() or None if output.returncode == 0 else None

    commit = run(["rev-parse", "HEAD"])
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"])
    dirty = bool(run(["status", "--porcelain"]))
    return commit, branch, dirty


def load_task(entrypoint: str) -> Callable[..., Any]:
    """Import `module:function`.

    Errors name both halves. A bare "ModuleNotFoundError: mypkg" leaves the reader
    guessing which suite field produced it.
    """
    module_name, _, attribute = entrypoint.partition(":")

    # The CLI exists to run the user's project code, and it is normally invoked from
    # that project's root. Python does not put the working directory on the path for
    # an installed console script, so without this every suite would need PYTHONPATH
    # set — a papercut with no upside.
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        msg = (
            f"cannot import module {module_name!r} from task entrypoint {entrypoint!r}: {exc}. "
            "Is it importable from the current working directory?"
        )
        raise RunError(msg) from exc

    target: Any = module
    for part in attribute.split("."):
        if not hasattr(target, part):
            msg = f"module {module_name!r} has no attribute {attribute!r}"
            raise RunError(msg)
        target = getattr(target, part)

    if not callable(target):
        msg = f"task entrypoint {entrypoint!r} is not callable"
        raise RunError(msg)
    return target  # type: ignore[no-any-return]


def load_dataset(loaded: LoadedSuite) -> Dataset:
    reference = loaded.suite.dataset
    if not reference.is_local:
        msg = (
            f"dataset {reference.name!r} lives on the server; run without --local, or point "
            "the suite at a local `path:` for offline use"
        )
        raise RunError(msg)

    assert reference.path is not None
    path = loaded.resolve_path(reference.path)
    dataset = Dataset.from_csv(path) if path.suffix.lower() == ".csv" else Dataset.from_jsonl(path)
    if reference.limit:
        dataset = dataset.limit(reference.limit)
    return dataset


def build_gate_set(loaded: LoadedSuite) -> GateSet | None:
    suite = loaded.suite
    if not suite.gates:
        return None

    rules: list[GateRule] = []
    for metric_key, spec in suite.gates.items():
        rules.append(
            GateRule(
                metric_key=metric_key.split("[")[0],
                minimum=spec.minimum,
                maximum=spec.maximum,
                max_absolute_regression=spec.max_regression,
                max_relative_regression=spec.max_relative_regression,
                severity=Severity.BLOCK if spec.blocking else Severity.WARN,
                slice=spec.slice,
                require_baseline=spec.require_baseline,
            )
        )
    return GateSet(
        name=suite.name,
        rules=rules,
        require_dataset_match=suite.baseline.require_dataset_match,
        # The mapping form is validated here rather than in the suite schema, so a bad
        # threshold names the gate field it came from instead of a pydantic union error.
        require_calibration=(
            suite.calibration.require
            if isinstance(suite.calibration.require, bool)
            else CalibrationRequirementSpec(**suite.calibration.require)
        ),
    )


def judge_metric_keys(loaded: LoadedSuite) -> list[str]:
    """Metric keys produced by LLM judges.

    The gate engine cannot work this out for itself — a `Metric` is a key and a number —
    and it needs to know, because gating on a judge nobody has checked is the specific
    thing calibration exists to make visible.
    """
    return [spec.name for spec in loaded.suite.evaluators if spec.type == "llm_judge"]


def resolve_calibrations(loaded: LoadedSuite) -> dict[str, CalibrationStatus]:
    """Load stored calibration evidence and re-check it against the current thresholds.

    Re-checked, not trusted. The stored record carries the `satisfied` verdict from when
    it was produced, but the suite's thresholds may have been tightened since. Reading
    the old boolean would make `min_kappa` decorative until somebody remembered to
    re-run a paid calibration.
    """
    suite = loaded.suite
    directory = loaded.resolve_path(suite.calibration.directory)
    records = load_all(directory)

    statuses: dict[str, CalibrationStatus] = {}
    for spec in suite.evaluators:
        if spec.type != "llm_judge":
            continue
        version = evaluator_version_hash(spec, rubric=load_rubric_text(spec, loaded))
        status = status_for(
            records, evaluator=spec.name, metric_key=spec.name, version_hash=version
        )
        if status.calibrated and not status.is_stale:
            status = _recheck(status, spec, records)
        statuses[spec.name] = status
    return statuses


def _recheck(
    status: CalibrationStatus, spec: Any, records: list[StoredCalibration]
) -> CalibrationStatus:
    record = next(
        r
        for r in records
        if r.evaluator == spec.name and r.version_hash == status.evaluator_version_hash
    )
    report = report_from_dict(record.report)
    check = check_requirement(report, requirement_for(spec))
    return status.model_copy(
        update={
            "satisfied": check.satisfied,
            "failures": list(check.failures),
            "warnings": list(check.warnings),
        }
    )


def requirement_for(spec: Any) -> CalibrationRequirement:
    """The thresholds one judge must meet, defaults filled in.

    `None` in the suite means "use the recommended default", not "no limit". A suite that
    omits `max_false_pass_rate` should still be protected against a judge that waves
    through work a human rejected.
    """
    default = CalibrationRequirement()
    calibration = spec.calibration
    if calibration is None:
        return default
    return CalibrationRequirement(
        min_agreement=_or(calibration.min_agreement, default.min_agreement),
        min_kappa=_or(calibration.min_kappa, default.min_kappa),
        max_false_pass_rate=_or(calibration.max_false_pass_rate, default.max_false_pass_rate),
        max_false_fail_rate=_or(calibration.max_false_fail_rate, default.max_false_fail_rate),
        min_examples=_or(calibration.min_examples, default.min_examples),
        min_per_class=_or(calibration.min_per_class, default.min_per_class),
        allow_position_bias=calibration.allow_position_bias,
    )


def _or(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


def plan_run(loaded: LoadedSuite) -> Plan:
    """Everything a run needs, resolved and checked, with zero model calls."""
    dataset = load_dataset(loaded)
    per_example, corpus = build_evaluators(loaded)

    return Plan(
        suite=loaded.suite.name,
        dataset=str(loaded.suite.dataset.path or loaded.suite.dataset.name),
        example_count=len(dataset),
        evaluator_names=[e.name for e in per_example],
        corpus_names=[c.name for c in corpus],
        gate_count=len(loaded.suite.gates),
        judge_calls=estimate_judge_calls(loaded, len(dataset)),
        baseline=loaded.suite.baseline.strategy,
        hints=loaded.hints,
    )


async def execute(
    loaded: LoadedSuite,
    *,
    models: Any = None,
    baseline_metrics: list[Metric] | None = None,
    journal: Path | None = None,
    resume: Path | None = None,
    limit: int | None = None,
) -> Outcome:
    suite = loaded.suite
    if suite.task is None:
        msg = f"suite {suite.name!r} declares no `task`, so there is nothing to run"
        raise RunError(msg)

    dataset = load_dataset(loaded)
    if limit:
        dataset = dataset.limit(limit)

    task = load_task(suite.task.entrypoint)
    per_example, corpus = build_evaluators(loaded)

    config = RunConfig(
        concurrency=suite.execution.concurrency,
        judge_concurrency=suite.execution.judge_concurrency,
        timeout_s=suite.task.timeout_s,
        retries=suite.task.retries,
        max_error_rate=suite.execution.max_error_rate,
        slice_by=suite.execution.slice_by,
        seed=suite.execution.seed,
        journal_path=journal,
        resume_from=resume,
        max_cost=Decimal(str(suite.execution.max_cost)) if suite.execution.max_cost else None,
    )

    result = await run_suite(
        dataset=dataset,
        task=task,
        evaluators=per_example,
        corpus_evaluators=corpus,
        gate_set=build_gate_set(loaded),
        baseline=baseline_metrics,
        models=models,
        config=config,
        suite_name=suite.name,
        judge_metrics=judge_metric_keys(loaded),
        calibrations=resolve_calibrations(loaded),
    )

    comparison = None
    if baseline_metrics:
        comparison = compare_metrics(
            result.metrics,
            baseline_metrics,
            candidate_results=result.results,
        )

    return Outcome(result=result, comparison=comparison)


def exit_code_for_setup_error() -> int:
    """Configuration problems are exit 3, distinct from a real gate failure.

    A CI job should be able to tell "your suite is broken" from "your change is
    worse", because they call for entirely different responses.
    """
    return ExitCode.CONFIGURATION_ERROR


__all__ = [
    "Outcome",
    "Plan",
    "RunError",
    "SuiteError",
    "build_gate_set",
    "execute",
    "exit_code_for_setup_error",
    "git_context",
    "load_dataset",
    "load_task",
    "plan_run",
]
