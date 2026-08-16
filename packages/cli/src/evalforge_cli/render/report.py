"""The machine-readable report.

`report_version` is explicit and validated on every write. The GitHub Action parses
this file, so it is a contract: an unannounced shape change would break other
people's CI silently, which is the one failure this product exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evalforge_core import EvalResult
from evalforge_core.compare import Comparison
from evalforge_types import Verdict

REPORT_VERSION = 1

REQUIRED_TOP_LEVEL = (
    "report_version",
    "suite",
    "verdict",
    "exit_code",
    "dataset",
    "metrics",
    "gates",
    "totals",
)


class ReportError(ValueError):
    """The report did not match its own contract."""


def build_report(
    result: EvalResult,
    *,
    comparison: Comparison | None = None,
    git_commit: str | None = None,
    git_branch: str | None = None,
    baseline_run_id: str | None = None,
    experiment_url: str | None = None,
    hints: list[str] | None = None,
) -> dict[str, Any]:
    deltas = {d.full_key: d for d in (comparison.deltas if comparison else [])}

    metrics: list[dict[str, Any]] = []
    for metric in sorted(result.metrics, key=lambda m: m.full_key):
        delta = deltas.get(metric.full_key)
        metrics.append(
            {
                "key": metric.key,
                "slice": metric.slice,
                "value": metric.value,
                "count": metric.count,
                # Reported separately from `count`, always. An errored evaluation is
                # not a score of zero, and a consumer that cannot see the difference
                # will draw the wrong conclusion.
                "error_count": metric.error_count,
                "unit": metric.unit,
                "ci_low": metric.ci_low,
                "ci_high": metric.ci_high,
                "baseline": delta.baseline if delta else None,
                "absolute_delta": delta.absolute_delta if delta else None,
                "relative_delta": delta.relative_delta if delta else None,
                "significant": delta.significant if delta else None,
            }
        )

    # Paired tests, when any ran. Keyed by metric so a consumer can join them to the gate results
    # without re-deriving which rule asked for what.
    significance = {
        key: {
            "test": test.test,
            "n_pairs": test.n_pairs,
            "difference": test.difference,
            "ci_low": test.ci_low,
            "ci_high": test.ci_high,
            "p_value": test.p_value,
            "adjusted_p_value": test.adjusted_p_value,
            "minimum_detectable_effect": test.minimum_detectable_effect,
            "dropped": test.dropped,
            "notes": list(test.notes),
        }
        for key, test in getattr(result, "significance", {}).items()
    }

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "significance": significance,
        "suite": result.suite,
        "verdict": result.gates.verdict.value,
        "exit_code": result.exit_code,
        "aborted_reason": result.aborted_reason,
        "git": {"commit": git_commit, "branch": git_branch},
        "dataset": {
            "name": result.dataset_name,
            "version": result.dataset_version,
            "content_hash": result.dataset_hash,
            "example_count": len(result.results),
        },
        "baseline": {
            "run_id": baseline_run_id,
            "dataset_match": comparison.dataset_match if comparison else None,
            "warnings": comparison.warnings if comparison else [],
        },
        "totals": {
            "examples": len(result.results),
            "errors": result.error_count,
            "duration_s": round(result.duration_s, 3),
            "total_cost": float(result.total_cost),
        },
        "metrics": metrics,
        "gates": [
            {
                "metric_key": gate.metric_key,
                "slice": gate.slice,
                "verdict": gate.verdict,
                "severity": gate.severity.value,
                "blocking": gate.blocking,
                "rule": gate.rule,
                "threshold": gate.threshold,
                "actual": gate.actual,
                "baseline": gate.baseline,
                "message": gate.message,
            }
            for gate in result.gates.results
        ],
        "regressed_examples": [
            {
                "example_id": r.example_id,
                "metric": r.metric,
                "baseline_score": r.baseline_score,
                "candidate_score": r.candidate_score,
                "trace_id": r.trace_id,
            }
            for r in (comparison.regressions[:100] if comparison else [])
        ],
        "failures": [
            {
                "example_id": r.example_id,
                "status": r.status.value,
                "error": r.error.message if r.error else None,
            }
            for r in result.failures()[:100]
        ],
        "hints": hints or [],
        "experiment_url": experiment_url,
    }

    validate_report(report)
    return report


def validate_report(report: dict[str, Any]) -> None:
    """Check the report against its own contract before anyone depends on it."""
    missing = [key for key in REQUIRED_TOP_LEVEL if key not in report]
    if missing:
        msg = f"report is missing required field(s): {', '.join(missing)}"
        raise ReportError(msg)

    if report["report_version"] != REPORT_VERSION:
        msg = f"report_version must be {REPORT_VERSION}, got {report['report_version']!r}"
        raise ReportError(msg)

    if report["verdict"] not in {v.value for v in Verdict}:
        msg = f"unknown verdict {report['verdict']!r}"
        raise ReportError(msg)

    # The exit code is what CI acts on, so a report whose verdict and exit code
    # disagree is worse than no report at all.
    expected = _exit_code_for(report["verdict"], report.get("aborted_reason"))
    if report["exit_code"] != expected:
        msg = (
            f"verdict {report['verdict']!r} implies exit code {expected}, "
            f"but the report says {report['exit_code']}"
        )
        raise ReportError(msg)


def _exit_code_for(verdict: str, aborted: str | None) -> int:
    if aborted:
        return 2
    return {"pass": 0, "warn": 0, "fail": 1, "error": 2}[verdict]


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    validate_report(report)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return target
