"""The terminal report.

Design rule: the reader must never have to open the YAML to interpret a failure. So
every gate row carries its threshold, and every regressed example carries the
concrete reason. A report that says "failed" and stops has moved the work rather
than done it.

Plain text, not Rich tables. This output lands in CI logs and in bug reports, where
box-drawing characters and ANSI escapes are noise. Colour is opt-in and disabled
under `CI` or `NO_COLOR`.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from proofstep_core import EvalResult
from proofstep_core.compare import Comparison
from proofstep_types import Verdict

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

MAX_LISTED_FAILURES = 10


def use_colour(stream: Any = None) -> bool:
    """Colour only when a human is plausibly reading it.

    `NO_COLOR` is honoured because it is the convention, and `CI` because escape
    codes in a build log make the log harder to read, not easier.
    """
    if os.environ.get("NO_COLOR") or os.environ.get("CI"):
        return False
    target = stream or sys.stdout
    return bool(getattr(target, "isatty", lambda: False)())


def use_unicode() -> bool:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in encoding


class Style:
    def __init__(self, *, colour: bool, unicode_: bool) -> None:
        self.colour = colour
        self.unicode = unicode_

    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.colour else text

    @property
    def tick(self) -> str:
        return "✓" if self.unicode else "PASS"

    @property
    def cross(self) -> str:
        return "✗" if self.unicode else "FAIL"

    @property
    def warn(self) -> str:
        return "⚠" if self.unicode else "WARN"

    @property
    def query(self) -> str:
        return "?" if self.unicode else "ERR"

    def mark(self, verdict: str) -> str:
        return {
            "pass": self.paint(self.tick, GREEN),
            "fail": self.paint(self.cross, RED),
            "warn": self.paint(self.warn, YELLOW),
            "error": self.paint(self.query, RED),
        }.get(verdict, verdict)


def render(
    result: EvalResult,
    *,
    comparison: Comparison | None = None,
    hints: list[str] | None = None,
    report_path: str | None = None,
    experiment_url: str | None = None,
    baseline_label: str | None = None,
    style: Style | None = None,
    verbose: bool = False,
) -> str:
    theme = style or Style(colour=use_colour(), unicode_=use_unicode())
    lines: list[str] = []

    lines.append(theme.paint(f"Proofstep · {result.suite}", BOLD))
    dataset = result.dataset_name or "<inline>"
    if result.dataset_version:
        dataset = f"{dataset}@{result.dataset_version}"
    lines.append(
        theme.paint(
            f"{dataset} ({len(result.results)} examples, sha {result.dataset_hash[:8]})", DIM
        )
    )
    lines.append(theme.paint(f"baseline {baseline_label or 'none'}", DIM))
    lines.append("")

    lines.extend(_metric_table(result, comparison, theme, verbose=verbose))

    if result.aborted_reason:
        lines.append("")
        lines.append(theme.paint(f"{theme.cross} run aborted: {result.aborted_reason}", RED))

    lines.extend(_warnings(comparison, hints, theme))
    lines.extend(_failures(result, comparison, theme))
    lines.extend(_footer(result, report_path, experiment_url, theme))
    return "\n".join(lines)


def _metric_table(
    result: EvalResult, comparison: Comparison | None, theme: Style, *, verbose: bool = False
) -> list[str]:
    deltas = {d.full_key: d for d in (comparison.deltas if comparison else [])}
    gates = {(g.metric_key, _slice_text(g.slice)): g for g in result.gates.results}

    # A suite that slices by a dimension produces one row per class per metric, which
    # buries four gates under forty rows. Show what someone is gating on plus the
    # unsliced headline numbers, and say how many were folded away — hidden is fine,
    # silently dropped is not.
    shown = [
        m
        for m in result.metrics
        if verbose or m.slice is None or (m.key, _slice_text(m.slice)) in gates
    ]
    hidden = len(result.metrics) - len(shown)

    rows: list[tuple[str, str, str, str, str, str]] = []
    for metric in sorted(shown, key=lambda m: m.full_key):
        delta = deltas.get(metric.full_key)
        gate = gates.get((metric.key, _slice_text(metric.slice)))

        baseline = _number(delta.baseline) if delta and delta.baseline is not None else "—"
        change = "—"
        if delta and delta.absolute_delta is not None:
            change = f"{delta.absolute_delta:+.4g}"

        verdict = theme.mark(gate.verdict) if gate else " "
        detail = _gate_detail(gate) if gate else ""
        if metric.error_count:
            # Surfaced inline: a metric computed from mostly-failed evaluations is
            # not the number it appears to be.
            detail = f"{detail}  [{metric.error_count} errored]".strip()

        rows.append((metric.full_key, baseline, _number(metric.value), change, verdict, detail))

    if not rows:
        return [theme.paint("no metrics were produced", YELLOW)]

    width = max(len(r[0]) for r in rows)
    width = min(max(width, 24), 44)

    header = f"{'METRIC'.ljust(width)}  {'BASELINE':>10}  {'CANDIDATE':>10}  {'DELTA':>9}  GATE"
    lines = [theme.paint(header, DIM)]
    for key, baseline, candidate, change, verdict, detail in rows:
        label = key if len(key) <= width else key[: width - 1] + "…"
        line = f"{label.ljust(width)}  {baseline:>10}  {candidate:>10}  {change:>9}  {verdict}"
        if detail:
            line = f"{line} {detail}"
        lines.append(line)

    if hidden:
        lines.append(
            theme.paint(f"{hidden} sliced metric(s) hidden; --verbose or see the JSON report", DIM)
        )
    return lines


def _gate_detail(gate: Any) -> str:
    """The threshold, inline, so nobody has to open the suite to read the row."""
    parts: list[str] = []
    if gate.rule == "minimum" or (gate.threshold is not None and gate.rule == "minimum"):
        parts.append(f"min {gate.threshold:.4g}")
    elif gate.rule == "maximum":
        parts.append(f"max {gate.threshold:.4g}")
    elif gate.rule in ("max_absolute_regression", "max_relative_regression"):
        parts.append(f"maxΔ {gate.threshold:.4g}")
    elif gate.rule == "error_rate":
        parts.append("evaluator errors")
    elif gate.rule == "metric_missing":
        parts.append("metric not produced")
    elif gate.rule == "no_data":
        parts.append("nothing measured")

    if not gate.blocking:
        parts.append("non-blocking")
    return f" {DIM}{', '.join(parts)}{RESET}" if parts and gate.severity else ", ".join(parts)


def _warnings(comparison: Comparison | None, hints: list[str] | None, theme: Style) -> list[str]:
    lines: list[str] = []
    for warning in comparison.warnings if comparison else []:
        lines.append("")
        lines.append(theme.paint(f"{theme.warn} {warning}", YELLOW))
    for hint in hints or []:
        lines.append("")
        lines.append(theme.paint(f"{theme.warn} {hint}", YELLOW))
    return lines


def _failures(result: EvalResult, comparison: Comparison | None, theme: Style) -> list[str]:
    lines: list[str] = []
    blocking = result.gates.blocking_failures
    warnings = result.gates.warnings

    summary: list[str] = []
    if blocking:
        summary.append(f"{len(blocking)} blocking failure{'s' if len(blocking) != 1 else ''}")
    if warnings:
        summary.append(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}")
    regressions = comparison.regressions if comparison else []
    if regressions:
        summary.append(
            f"{len(regressions)} regressed example{'s' if len(regressions) != 1 else ''}"
        )
    if result.error_count:
        summary.append(
            f"{result.error_count} failed example{'s' if result.error_count != 1 else ''}"
        )

    if summary:
        lines.append("")
        lines.append(" · ".join(summary))

    for gate in blocking:
        lines.append("")
        lines.append(f"{theme.mark(gate.verdict)} {gate.metric_key}  {gate.message}")

    # Concrete examples, not just aggregate movement: "which one broke" is the first
    # question anyone asks, and answering it in the report saves a round trip.
    for regression in regressions[:MAX_LISTED_FAILURES]:
        lines.append(
            f"  {theme.paint(theme.cross, RED)} {regression.metric:<24} {regression.example_id}"
            f"  {_number(regression.baseline_score)} → {_number(regression.candidate_score)}"
        )
    if len(regressions) > MAX_LISTED_FAILURES:
        remaining = len(regressions) - MAX_LISTED_FAILURES
        lines.append(theme.paint(f"  … {remaining} more in the JSON report", DIM))

    for failure in result.failures()[:MAX_LISTED_FAILURES]:
        message = failure.error.message if failure.error else failure.status.value
        lines.append(f"  {theme.paint(theme.query, RED)} {failure.example_id}  {message}")

    return lines


def _footer(
    result: EvalResult, report_path: str | None, experiment_url: str | None, theme: Style
) -> list[str]:
    lines = [""]
    cost = float(result.total_cost)
    lines.append(
        theme.paint(
            f"{len(result.results)} examples in {result.duration_s:.1f}s · ${cost:.4f}", DIM
        )
    )
    if report_path:
        lines.append(theme.paint(f"Report: {report_path}", DIM))
    if experiment_url:
        lines.append(theme.paint(f"Experiment: {experiment_url}", DIM))

    verdict = result.gates.verdict
    colour = {Verdict.PASS: GREEN, Verdict.WARN: YELLOW}.get(verdict, RED)
    lines.append(theme.paint(f"{verdict.value} (exit {result.exit_code})", colour))
    return lines


def _slice_text(slice_: dict[str, str] | None) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(slice_.items())) if slice_ else ""


def _number(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000 or (value and abs(value) < 0.001):
        return f"{value:.4g}"
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"
