"""The `evalforge calibrate` command: planning, running, and storing.

Kept out of `main.py` so the logic is testable without Typer, and out of
`evaluation-core` because it touches the filesystem and imports user modules.

Two paths to a report:

- **live** — call the judge over the labelled set, which costs money
- **`--verdicts`** — recompute from recorded verdicts, which costs nothing

The second is not a testing convenience bolted on. Changing a threshold, fixing a bug in
the maths, or adding a second annotator should not require paying to re-run a judge that
already answered. It also means this whole path is exercised in CI with no provider
credential, which is the only way `require_calibration` can be trusted to work on a fork
pull request.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from evalforge_cli.calibration_store import evaluator_version_hash, write_calibration
from evalforge_cli.registry import load_rubric_text
from evalforge_cli.runner import requirement_for
from evalforge_cli.suite.loader import LoadedSuite
from evalforge_cli.suite.schema import EvaluatorSpec
from evalforge_core.calibration import (
    CalibrationReport,
    CalibrationRequirement,
    JudgeVerdict,
    RequirementCheck,
    calibrate,
)
from evalforge_core.calibration_runner import (
    CalibrationCase,
    CalibrationDataError,
    assert_judge_cannot_see_labels,
    load_calibration_set,
    report_to_dict,
    run_calibration,
    summarize_labels,
    total_cost_estimate,
)
from evalforge_core.evaluators.judge import LLMJudge


class CalibrationCommandError(ValueError):
    """Anything that stops a calibration before it starts costing money."""


@dataclass(frozen=True, slots=True)
class Plan:
    loaded: LoadedSuite
    spec: EvaluatorSpec
    judge: LLMJudge
    cases: list[CalibrationCase]
    labels_path: Path
    labels_hash: str
    version_hash: str
    requirement: CalibrationRequirement
    passing_labels: list[str]
    ordinal_order: list[str] | None

    @property
    def judge_calls(self) -> int:
        return total_cost_estimate(self.cases, self.judge)

    @property
    def label_summary(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in summarize_labels(self.cases).items())


def plan(loaded: LoadedSuite, *, evaluator: str, labels: Path | None) -> Plan:
    """Resolve everything a calibration run needs, without calling anything."""
    spec = next((e for e in loaded.suite.evaluators if e.name == evaluator), None)
    if spec is None:
        available = ", ".join(e.name for e in loaded.suite.evaluators) or "<none>"
        msg = f"suite has no evaluator named {evaluator!r}. Available: {available}"
        raise CalibrationCommandError(msg)
    if spec.type != "llm_judge":
        # Calibration measures whether an *opinion* matches a human's. A deterministic
        # check has no opinion — `exact_match` either matched or it did not — so
        # calibrating one would produce a meaningless certificate.
        msg = (
            f"evaluator {evaluator!r} is type {spec.type!r}, not 'llm_judge'. Only judges "
            "need calibration: a deterministic check has no opinion to validate."
        )
        raise CalibrationCommandError(msg)

    labels_path = _resolve_labels(loaded, spec, labels)
    try:
        cases = load_calibration_set(labels_path)
    except CalibrationDataError as exc:
        raise CalibrationCommandError(str(exc)) from exc

    rubric = load_rubric_text(spec, loaded)
    judge = _build_judge(spec, rubric)
    try:
        assert_judge_cannot_see_labels(judge)
    except CalibrationDataError as exc:
        raise CalibrationCommandError(str(exc)) from exc

    passing = list(spec.calibration.passing_labels) if spec.calibration else []
    return Plan(
        loaded=loaded,
        spec=spec,
        judge=judge,
        cases=cases,
        labels_path=labels_path,
        labels_hash=_hash_file(labels_path),
        version_hash=evaluator_version_hash(spec, rubric=rubric),
        requirement=requirement_for(spec),
        passing_labels=passing,
        ordinal_order=_ordinal_order(spec),
    )


def _resolve_labels(loaded: LoadedSuite, spec: EvaluatorSpec, override: Path | None) -> Path:
    if override is not None:
        path = override if override.is_absolute() else Path.cwd() / override
        if not path.exists():
            msg = f"labelled set not found: {path}"
            raise CalibrationCommandError(msg)
        return path

    if spec.calibration is None or not spec.calibration.dataset:
        msg = (
            f"judge {spec.name!r} declares no `calibration.dataset`, and no --labels was "
            "given. Point one of them at a human-labelled JSONL file."
        )
        raise CalibrationCommandError(msg)
    path = loaded.resolve_path(spec.calibration.dataset)
    if not path.exists():
        msg = f"labelled set not found: {path} (from calibration.dataset)"
        raise CalibrationCommandError(msg)
    return path


def _build_judge(spec: EvaluatorSpec, rubric: str) -> LLMJudge:
    assert spec.model is not None  # guaranteed by suite validation
    return LLMJudge(
        name=spec.name,
        rubric=rubric,
        model=spec.model,
        inputs=spec.inputs,
        mode="classify" if spec.labels else "rubric",
        labels=spec.labels or None,
        scale=(spec.scale.min, spec.scale.max),
        normalize=spec.scale.normalize,
        temperature=spec.temperature,
        seed=spec.seed,
        votes=spec.votes,
        timeout_s=spec.timeout_s,
        max_retries=spec.max_retries,
    )


def _ordinal_order(spec: EvaluatorSpec) -> list[str] | None:
    """The rubric scale as ordered labels, or None for a classifier.

    A classifier's labels have no order — "spam" is not between "ham" and "promo" — so
    weighting a near miss would be meaningless. A 1-5 rubric does, and treating "4 where
    the human said 5" as a total miss is not a defensible way to grade a scale.
    """
    if spec.labels:
        return None
    return [str(value) for value in range(spec.scale.min, spec.scale.max + 1)]


def produce(
    plan_: Plan,
    *,
    verdicts_path: Path | None,
    model_client: str | None,
    concurrency: int,
) -> CalibrationReport:
    """Get a report, either from recorded verdicts or by running the judge."""
    if verdicts_path is not None:
        verdicts = _load_verdicts(verdicts_path)
        return calibrate(
            [case.labelled for case in plan_.cases],
            verdicts,
            passing_labels=plan_.passing_labels or None,
            ordinal_order=plan_.ordinal_order,
        )

    models = load_model_client(model_client)
    return asyncio.run(
        run_calibration(
            plan_.judge,
            plan_.cases,
            models,
            concurrency=concurrency,
            passing_labels=plan_.passing_labels or None,
            ordinal_order=plan_.ordinal_order,
        )
    )


def _load_verdicts(path: Path) -> list[JudgeVerdict]:
    """Read recorded judge verdicts.

    One JSON object per line: `{"id": ..., "label": ..., "cost": ..., "latency_ms": ...}`,
    or `{"id": ..., "error": "..."}` for a call that failed. An errored verdict is
    preserved as an error rather than dropped: excluding failures silently would make a
    judge that times out on hard examples look better than one that answers them badly.
    """
    if not path.exists():
        msg = f"verdicts file not found: {path}"
        raise CalibrationCommandError(msg)

    verdicts: list[JudgeVerdict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"{path}:{number}: not valid JSON — {exc}"
            raise CalibrationCommandError(msg) from exc
        if not isinstance(row, dict) or not row.get("id"):
            msg = f"{path}:{number}: every verdict needs an 'id'"
            raise CalibrationCommandError(msg)

        error = row.get("error")
        verdicts.append(
            JudgeVerdict(
                example_id=str(row["id"]),
                label=None if error else _optional_str(row.get("label")),
                errored=bool(error),
                error=str(error) if error else None,
                cost=Decimal(str(row.get("cost", "0"))),
                latency_ms=int(row.get("latency_ms") or 0),
            )
        )
    if not verdicts:
        msg = f"{path} contains no verdicts"
        raise CalibrationCommandError(msg)
    return verdicts


def _optional_str(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def load_model_client(entrypoint: str | None) -> Any:
    """Import a `module:factory` that returns a ModelClient.

    Provider adapters are not part of this package — `evaluation-core` must stay free of
    provider SDKs for local mode to work at all — so the judge's model access is supplied
    by the project being evaluated.
    """
    target = entrypoint or os.environ.get("EVALFORGE_MODEL_CLIENT")
    if not target:
        msg = (
            "no model client. Pass --model-client module:factory, set "
            "EVALFORGE_MODEL_CLIENT, or use --verdicts to recompute from recorded "
            "judge output without calling a model."
        )
        raise CalibrationCommandError(msg)

    module_name, _, attribute = target.partition(":")
    if not attribute:
        msg = f"--model-client must be 'module:factory', got {target!r}"
        raise CalibrationCommandError(msg)

    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        msg = f"cannot import model client module {module_name!r}: {exc}"
        raise CalibrationCommandError(msg) from exc

    factory = getattr(module, attribute, None)
    if factory is None:
        msg = f"module {module_name!r} has no attribute {attribute!r}"
        raise CalibrationCommandError(msg)
    return factory() if callable(factory) else factory


def store(plan_: Plan, report: CalibrationReport, check: RequirementCheck) -> Path:
    """Write the record CI will read."""
    directory = plan_.loaded.resolve_path(plan_.loaded.suite.calibration.directory)
    return write_calibration(
        directory,
        evaluator=plan_.spec.name,
        version_hash=plan_.version_hash,
        report=report_to_dict(report),
        requirement={
            "min_agreement": plan_.requirement.min_agreement,
            "min_kappa": plan_.requirement.min_kappa,
            "max_false_pass_rate": plan_.requirement.max_false_pass_rate,
            "max_false_fail_rate": plan_.requirement.max_false_fail_rate,
            "min_examples": plan_.requirement.min_examples,
            "min_per_class": plan_.requirement.min_per_class,
            "allow_position_bias": plan_.requirement.allow_position_bias,
        },
        satisfied=check.satisfied,
        failures=list(check.failures),
        warnings=list(check.warnings),
        labels_path=str(plan_.labels_path.name),
        labels_hash=plan_.labels_hash,
    )


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
