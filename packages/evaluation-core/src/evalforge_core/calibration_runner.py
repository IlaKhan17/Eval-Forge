"""Running a judge against a labelled calibration set.

Separate from `calibration.py` so the maths stays pure and offline-testable. This module
is the part that costs money.

The calibration set is an ordinary dataset file with human labels attached, which means
`evalforge calibrate` reads the same JSONL a suite reads. That is deliberate: a
calibration set built from production traces should be promotable to a golden dataset
without a format conversion, and a separate schema would guarantee the two drift apart.

The one non-obvious rule here: **the judge is never shown the human label.** It is
passed the same fields it would see in a real run, resolved through the same allow-list.
A judge that can see the answer key during calibration produces a report certifying
nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from evalforge_core.calibration import (
    CalibrationReport,
    ClassBreakdown,
    ConfusionMatrix,
    JudgeVerdict,
    LabelledExample,
    PairwiseProbe,
    PositionBiasReport,
    calibrate,
)
from evalforge_core.evaluators.judge import LLMJudge
from evalforge_core.types import EvalContext, ModelClient
from evalforge_types import Example

#: Field in `metadata` holding the primary human label.
LABEL_KEY = "human_label"
SECOND_LABEL_KEY = "second_human_label"
ADJUDICATED_KEY = "adjudicated_label"
#: Where the judge's own output lives when the set was built from recorded outputs.
OUTPUT_KEY = "output"


class CalibrationDataError(ValueError):
    """The calibration file cannot be used. Always names the offending example."""


@dataclass(frozen=True, slots=True)
class CalibrationCase:
    """One calibration example: the labels, plus the payload the judge will see."""

    labelled: LabelledExample
    example: Example
    output: Any


def load_calibration_set(path: str | Path) -> list[CalibrationCase]:
    """Read a labelled JSONL calibration file.

    Refuses rather than skips on a missing label. A calibration run that quietly drops
    the unlabelled half of the file reports agreement over whatever happened to be
    labelled, and that number will be used to justify gating a merge.
    """
    source = Path(path)
    if not source.exists():
        msg = f"calibration set not found: {source}"
        raise CalibrationDataError(msg)

    cases: list[CalibrationCase] = []
    seen: set[str] = set()
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            msg = f"{source}:{number}: not valid JSON — {exc}"
            raise CalibrationDataError(msg) from exc
        if not isinstance(row, dict):
            msg = f"{source}:{number}: expected a JSON object, got {type(row).__name__}"
            raise CalibrationDataError(msg)

        cases.append(_to_case(row, source=str(source), number=number, seen=seen))

    if not cases:
        msg = f"{source} contains no examples"
        raise CalibrationDataError(msg)
    return cases


def _to_case(row: dict[str, Any], *, source: str, number: int, seen: set[str]) -> CalibrationCase:
    example_id = str(row.get("id") or f"line-{number}")
    if example_id in seen:
        # Duplicate ids would double-count an example in every rate in the report.
        msg = f"{source}:{number}: duplicate example id {example_id!r}"
        raise CalibrationDataError(msg)
    seen.add(example_id)

    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        msg = f"{source}:{number}: metadata must be an object"
        raise CalibrationDataError(msg)

    label = row.get(LABEL_KEY, metadata.get(LABEL_KEY))
    if label is None or str(label) == "":
        msg = (
            f"{source}:{number}: example {example_id!r} has no {LABEL_KEY!r}. "
            "Every calibration example needs a human label — that is the whole point "
            "of the file. Remove the example or label it."
        )
        raise CalibrationDataError(msg)

    output = row.get(OUTPUT_KEY, metadata.get(OUTPUT_KEY))
    text = output if isinstance(output, str) else json.dumps(output, default=str)

    return CalibrationCase(
        labelled=LabelledExample(
            example_id=example_id,
            human_label=str(label),
            second_human_label=_optional_str(
                row.get(SECOND_LABEL_KEY, metadata.get(SECOND_LABEL_KEY))
            ),
            adjudicated_label=_optional_str(
                row.get(ADJUDICATED_KEY, metadata.get(ADJUDICATED_KEY))
            ),
            output_length=len(text) if output is not None else None,
            metadata={k: str(v) for k, v in metadata.items()},
        ),
        example=Example(
            id=example_id,
            input=row.get("input") or {},
            # `expected` is carried so the file can double as a golden dataset, but the
            # judge's `inputs` allow-list is what decides whether it is ever sent — and
            # a judge that declares `expected.*` is rejected below.
            expected=row.get("expected"),
            metadata=metadata,
        ),
        output=output,
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def assert_judge_cannot_see_labels(judge: LLMJudge) -> None:
    """Refuse to calibrate a judge that reads the answer key.

    A judge whose declared inputs include `expected.*` — or the label fields themselves
    — will agree with the humans almost perfectly, and the resulting report is a
    certificate for a leak. Better to fail loudly at the start of a paid run than to
    produce a number that means the opposite of what it appears to.
    """
    forbidden = [
        path
        for path in judge.inputs
        if path.startswith("expected")
        or LABEL_KEY in path
        or SECOND_LABEL_KEY in path
        or ADJUDICATED_KEY in path
    ]
    if forbidden:
        msg = (
            f"judge {judge.name!r} declares input(s) {forbidden} which expose the human "
            "label. Calibrating against labels the judge can read measures nothing. "
            "Remove them from `inputs`."
        )
        raise CalibrationDataError(msg)


async def run_calibration(
    judge: LLMJudge,
    cases: Sequence[CalibrationCase],
    models: ModelClient,
    *,
    concurrency: int = 4,
    passing_labels: Sequence[str] | None = None,
    ordinal_order: Sequence[str] | None = None,
    probes: Sequence[PairwiseProbe] = (),
    seed: int = 42,
    on_progress: Any = None,
) -> CalibrationReport:
    """Score every case with the judge, then compute the report.

    Concurrency is bounded and separate from any task concurrency: this contends for the
    judge provider's rate limit and nothing else.
    """
    assert_judge_cannot_see_labels(judge)

    limit = asyncio.Semaphore(max(1, concurrency))
    verdicts: list[JudgeVerdict] = []

    async def one(case: CalibrationCase) -> JudgeVerdict:
        async with limit:
            context = EvalContext(
                example=case.example,
                output=case.output,
                # Withheld deliberately, belt as well as braces: even if a judge slipped
                # past the allow-list check, there is nothing here to read.
                expected=None,
                metadata=dict(case.example.metadata),
                models=models,
                seed=seed,
            )
            score = await judge.evaluate(context)
            return _to_verdict(case.labelled.example_id, score, ordinal_order=ordinal_order)

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(one(case)) for case in cases]
    for index, task in enumerate(tasks):
        verdicts.append(task.result())
        if on_progress is not None:
            with contextlib.suppress(Exception):
                # Progress reporting is a convenience; a broken callback must not lose a
                # paid calibration run.
                on_progress(index + 1, len(cases))

    return calibrate(
        [case.labelled for case in cases],
        verdicts,
        passing_labels=passing_labels,
        ordinal_order=ordinal_order,
        probes=probes,
        seed=seed,
    )


def _to_verdict(
    example_id: str, score: Any, *, ordinal_order: Sequence[str] | None
) -> JudgeVerdict:
    """Project a `Score` onto the label space the human labels live in."""
    if score.errored:
        return JudgeVerdict(
            example_id,
            errored=True,
            error=score.error,
            cost=score.cost,
            latency_ms=score.latency_ms,
        )

    if score.label is not None:
        label: str | None = str(score.label)
    elif score.passed is not None:
        label = "pass" if score.passed else "fail"
    elif score.value is not None and ordinal_order:
        label = _nearest_ordinal(score, ordinal_order)
    elif score.value is not None:
        # A bare numeric score with no declared scale has no label to compare against.
        # Treated as an error rather than rounded to something arbitrary.
        return JudgeVerdict(
            example_id,
            errored=True,
            error=(
                "judge returned a numeric score but the calibration set declares no "
                "ordinal scale, so there is nothing to compare it to"
            ),
            cost=score.cost,
            latency_ms=score.latency_ms,
        )
    else:
        label = None

    return JudgeVerdict(
        example_id,
        label=label,
        errored=label is None,
        error=None if label else "judge produced no label",
        cost=score.cost,
        latency_ms=score.latency_ms,
    )


def _nearest_ordinal(score: Any, order: Sequence[str]) -> str:
    """Recover the rubric point from a normalized score.

    A rubric judge normalizes 1-5 onto [0, 1], so the raw point has to be recovered to
    compare against a human label of "4". The raw value is used when the judge recorded
    it; otherwise the normalized value is mapped back onto the scale.
    """
    raw = (score.raw or {}).get("raw_score") if isinstance(score.raw, dict) else None
    if raw is not None and str(raw) in order:
        return str(raw)

    index = round(float(score.value) * (len(order) - 1))
    return order[max(0, min(len(order) - 1, index))]


def summarize_labels(cases: Iterable[CalibrationCase]) -> dict[str, int]:
    """Human label distribution, for the "is this set balanced" question."""
    counts: dict[str, int] = {}
    for case in cases:
        key = case.labelled.reference_label
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def report_to_dict(report: CalibrationReport) -> dict[str, Any]:
    """JSON-serializable form, for storage and for the CLI's `--output`."""
    return {
        "n_examples": report.n_examples,
        "n_errored": report.n_errored,
        "error_rate": report.error_rate,
        "agreement": report.agreement,
        "agreement_ci": list(report.agreement_ci),
        "kappa": report.kappa,
        "kappa_ci": list(report.kappa_ci) if report.kappa_ci else None,
        "kappa_kind": report.kappa_kind,
        "kappa_undefined_reason": report.kappa_undefined_reason,
        "false_pass_rate": report.false_pass_rate,
        "false_fail_rate": report.false_fail_rate,
        "human_kappa": report.human_kappa,
        "judge_kappa_on_ceiling_subset": report.judge_kappa_on_ceiling_subset,
        "n_ceiling_examples": report.n_ceiling_examples,
        "at_human_ceiling": report.at_human_ceiling,
        "leniency": report.leniency,
        "scale_compression": report.scale_compression,
        "verbosity_bias": report.verbosity_bias,
        "mean_cost": str(report.mean_cost),
        "total_cost": str(report.total_cost),
        "p50_latency_ms": report.p50_latency_ms,
        "p95_latency_ms": report.p95_latency_ms,
        "confusion": {
            "labels": list(report.confusion.labels),
            # Tuple keys are not JSON-representable; a flat list of rows keeps the
            # matrix readable in a diff.
            "counts": [
                {"human": human, "judge": judge, "count": count}
                for (human, judge), count in sorted(report.confusion.counts.items())
            ],
        },
        "per_class": [
            {
                "label": c.label,
                "support": c.support,
                "recall": c.recall,
                "precision": c.precision,
                "top_confusion": list(c.top_confusion) if c.top_confusion else None,
            }
            for c in report.per_class
        ],
        "position_bias": (
            {
                "n_pairs": report.position_bias.n_pairs,
                "inconsistency_rate": report.position_bias.inconsistency_rate,
                "first_position_rate": report.position_bias.first_position_rate,
                "n_unresolved": report.position_bias.n_unresolved,
                "biased": report.position_bias.biased,
            }
            if report.position_bias
            else None
        ),
        "notes": list(report.notes),
    }


def total_cost_estimate(cases: Sequence[CalibrationCase], judge: LLMJudge) -> int:
    """Number of judge calls a calibration run will make.

    Reported before spending anything, because `votes: 5` on a 200-example set is a
    thousand calls and that should be a decision rather than a surprise.
    """
    return len(cases) * max(1, judge.votes)


__all__ = [
    "CalibrationCase",
    "CalibrationDataError",
    "assert_judge_cannot_see_labels",
    "load_calibration_set",
    "report_from_dict",
    "report_to_dict",
    "run_calibration",
    "summarize_labels",
    "total_cost_estimate",
]


def report_from_dict(payload: dict[str, Any]) -> CalibrationReport:
    """Rebuild a report from its stored form.

    Needed so a gate can re-apply the *current* thresholds to *stored* evidence. The
    alternative — trusting the `satisfied` boolean written when the calibration ran —
    means tightening `min_kappa` in a suite has no effect until somebody remembers to
    re-run the calibration, which is exactly the kind of silent no-op that makes a
    threshold decorative.

    Tolerant of missing fields: a record written by an older version should degrade to
    "some checks cannot be applied", not crash the run that reads it.
    """
    confusion_counts: dict[tuple[str, str], int] = {}
    raw_confusion = payload.get("confusion") or {}
    for row in raw_confusion.get("counts") or []:
        confusion_counts[str(row.get("human")), str(row.get("judge"))] = int(row.get("count") or 0)

    per_class = tuple(
        ClassBreakdown(
            label=str(row.get("label", "")),
            support=int(row.get("support") or 0),
            recall=float(row.get("recall") or 0.0),
            precision=float(row.get("precision") or 0.0),
            top_confusion=(
                (str(row["top_confusion"][0]), int(row["top_confusion"][1]))
                if row.get("top_confusion")
                else None
            ),
        )
        for row in payload.get("per_class") or []
    )

    bias_payload = payload.get("position_bias")
    bias = (
        PositionBiasReport(
            n_pairs=int(bias_payload.get("n_pairs") or 0),
            inconsistency_rate=float(bias_payload.get("inconsistency_rate") or 0.0),
            first_position_rate=float(bias_payload.get("first_position_rate") or 0.0),
            n_unresolved=int(bias_payload.get("n_unresolved") or 0),
        )
        if isinstance(bias_payload, dict)
        else None
    )

    return CalibrationReport(
        n_examples=int(payload.get("n_examples") or 0),
        n_errored=int(payload.get("n_errored") or 0),
        agreement=float(payload.get("agreement") or 0.0),
        agreement_ci=_pair(payload.get("agreement_ci")) or (0.0, 1.0),
        kappa=_float_or_none(payload.get("kappa")),
        kappa_ci=_pair(payload.get("kappa_ci")),
        kappa_kind=payload.get("kappa_kind") or "unweighted",
        kappa_undefined_reason=payload.get("kappa_undefined_reason"),
        false_pass_rate=_float_or_none(payload.get("false_pass_rate")),
        false_fail_rate=_float_or_none(payload.get("false_fail_rate")),
        confusion=ConfusionMatrix(
            labels=tuple(raw_confusion.get("labels") or ()), counts=confusion_counts
        ),
        per_class=per_class,
        human_kappa=_float_or_none(payload.get("human_kappa")),
        judge_kappa_on_ceiling_subset=_float_or_none(payload.get("judge_kappa_on_ceiling_subset")),
        n_ceiling_examples=int(payload.get("n_ceiling_examples") or 0),
        mean_cost=Decimal(str(payload.get("mean_cost") or "0")),
        total_cost=Decimal(str(payload.get("total_cost") or "0")),
        p50_latency_ms=float(payload.get("p50_latency_ms") or 0.0),
        p95_latency_ms=float(payload.get("p95_latency_ms") or 0.0),
        leniency=_float_or_none(payload.get("leniency")),
        scale_compression=_float_or_none(payload.get("scale_compression")),
        verbosity_bias=_float_or_none(payload.get("verbosity_bias")),
        position_bias=bias,
        notes=tuple(payload.get("notes") or ()),
    )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pair(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, list | tuple) or len(value) != 2:
        return None
    low, high = _float_or_none(value[0]), _float_or_none(value[1])
    return None if low is None or high is None else (low, high)
