"""Reading and writing calibration evidence as files in the repository.

Calibration reports are **committed to git**, not kept only in a server. Three reasons,
in order of importance:

1. `require_calibration` has to work in CI with no server and no credentials. The
   offline-suite story (docs/GITHUB_ACTIONS.md) is what makes fork pull requests safe,
   and a gate that silently degrades to "uncalibrated" whenever the server is
   unreachable would be worse than no gate.
2. A calibration is evidence about a judgement, and evidence belongs in review. A
   reviewer should see "this rubric change also changed the false-pass rate from 0.02 to
   0.14" in the diff.
3. The filename carries the evaluator's config hash, so a rubric edit produces a *new*
   filename. The old report stays put and the gate says "stale" instead of quietly
   applying yesterday's evidence to today's judge.

The server copy (Phase 4c tables) remains the source of truth for dashboards and
cross-run history. This is the copy CI reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from proofstep_core.versioning import config_hash
from proofstep_types import CalibrationStatus

DEFAULT_DIRECTORY = "calibrations"
SUFFIX = ".calibration.json"


class CalibrationStoreError(ValueError):
    """A stored calibration file could not be read."""


@dataclass(frozen=True, slots=True)
class StoredCalibration:
    path: Path
    evaluator: str
    version_hash: str
    report: dict[str, Any]
    requirement: dict[str, Any]
    satisfied: bool
    failures: list[str]
    warnings: list[str]
    calibrated_at: datetime | None


def filename(evaluator: str, version_hash: str) -> str:
    return f"{evaluator}.{version_hash}{SUFFIX}"


def write_calibration(
    directory: Path,
    *,
    evaluator: str,
    version_hash: str,
    report: dict[str, Any],
    requirement: dict[str, Any],
    satisfied: bool,
    failures: list[str],
    warnings: list[str],
    labels_path: str | None = None,
    labels_hash: str | None = None,
) -> Path:
    """Write one calibration record, returning its path."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename(evaluator, version_hash)

    payload = {
        "schema": 1,
        "evaluator": evaluator,
        "evaluator_version_hash": version_hash,
        "calibrated_at": datetime.now(UTC).isoformat(),
        # Recorded so a reviewer can tell whether a changed number came from a changed
        # judge or a changed labelled set. Without it, "agreement fell to 0.7" is
        # unattributable.
        "labels_path": labels_path,
        "labels_hash": labels_hash,
        "requirement": requirement,
        "satisfied": satisfied,
        "failures": failures,
        "warnings": warnings,
        "report": report,
    }
    # Indented and newline-terminated: this file is read in pull requests.
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def read_calibration(path: Path) -> StoredCalibration:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"{path}: cannot read calibration record — {exc}"
        raise CalibrationStoreError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"{path}: expected a JSON object"
        raise CalibrationStoreError(msg)

    stamp = payload.get("calibrated_at")
    when: datetime | None = None
    if isinstance(stamp, str):
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            when = None

    return StoredCalibration(
        path=path,
        evaluator=str(payload.get("evaluator", "")),
        version_hash=str(payload.get("evaluator_version_hash", "")),
        report=payload.get("report") or {},
        requirement=payload.get("requirement") or {},
        satisfied=bool(payload.get("satisfied")),
        failures=list(payload.get("failures") or []),
        warnings=list(payload.get("warnings") or []),
        calibrated_at=when,
    )


def load_all(directory: Path) -> list[StoredCalibration]:
    """Every calibration record in a directory, newest first.

    A malformed file is an error rather than a skip. Skipping it would present as "this
    judge was never calibrated", which is the same signal as a missing file and hides a
    fixable problem.
    """
    if not directory.exists():
        return []
    records = [read_calibration(path) for path in sorted(directory.glob(f"*{SUFFIX}"))]
    records.sort(key=lambda r: r.calibrated_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return records


def status_for(
    records: list[StoredCalibration], *, evaluator: str, metric_key: str, version_hash: str
) -> CalibrationStatus:
    """Resolve one judge's calibration state, including staleness.

    Three outcomes: a record for this exact version; a record for a *different* version
    of the same evaluator (stale — the rubric, model, or parameters changed, so the old
    evidence does not describe this judge); or nothing at all.
    """
    for record in records:
        if record.evaluator == evaluator and record.version_hash == version_hash:
            report = record.report
            return CalibrationStatus(
                metric_key=metric_key,
                evaluator_name=evaluator,
                evaluator_version_hash=version_hash,
                calibrated=True,
                satisfied=record.satisfied,
                failures=record.failures,
                warnings=record.warnings,
                n_examples=int(report.get("n_examples") or 0),
                agreement=_opt_float(report.get("agreement")),
                kappa=_opt_float(report.get("kappa")),
                false_pass_rate=_opt_float(report.get("false_pass_rate")),
                at_human_ceiling=bool(report.get("at_human_ceiling")),
                calibrated_at=record.calibrated_at,
            )

    stale = next((r for r in records if r.evaluator == evaluator), None)
    if stale is not None:
        return CalibrationStatus(
            metric_key=metric_key,
            evaluator_name=evaluator,
            evaluator_version_hash=stale.version_hash,
            calibrated=True,
            satisfied=stale.satisfied,
            n_examples=int((stale.report or {}).get("n_examples") or 0),
            kappa=_opt_float((stale.report or {}).get("kappa")),
            stale_for_version=version_hash,
        )

    return CalibrationStatus(metric_key=metric_key, evaluator_name=evaluator, calibrated=False)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluator_version_hash(spec: Any, *, rubric: str) -> str:
    """The config hash of a judge, as the gate engine and the server both compute it.

    The rubric *text* goes in, not its path. Editing `rubrics/groundedness.md` in place
    changes the metric's definition, so it must change the version — otherwise a rubric
    edit silently redefines the ruler while keeping the old calibration's blessing, which
    is precisely the "rubric drift" failure this system is meant to catch.
    """
    return config_hash(
        {
            "type": spec.type,
            "name": spec.name,
            "mode": "classify" if spec.labels else "rubric",
            "model": spec.model,
            "rubric": rubric,
            "inputs": sorted(spec.inputs),
            "labels": sorted(spec.labels) if spec.labels else None,
            "scale": [spec.scale.min, spec.scale.max, spec.scale.normalize],
            "temperature": spec.temperature,
            "seed": spec.seed,
            "votes": spec.votes,
        }
    )
