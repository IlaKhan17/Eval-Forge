"""Render a report as the pull-request comment.

Written from the JSON report rather than from live objects, so the Action can post a
comment for a run it did not execute — and so this renderer is a pure function of a
file, which makes it snapshot-testable with no GitHub involved.

Two constraints shape the output:

- **GitHub caps a comment at 65,536 characters.** Exceeding it fails the API call,
  which would mean no comment at all. Truncation is therefore deliberate, ordered
  worst-first, and always states what was dropped.
- **The reader is skimming a PR.** The verdict and the blocking reason must be
  visible without expanding anything; everything else goes in `<details>`.
"""

from __future__ import annotations

from typing import Any

# Comments are found and updated by this marker rather than by author or position,
# so a re-run edits its own comment instead of appending a new one.
MARKER = "<!-- proofstep-report -->"

GITHUB_COMMENT_LIMIT = 65_536
TRUNCATION_BUDGET = GITHUB_COMMENT_LIMIT - 2_000

VERDICT_HEADLINE = {
    "pass": ("✅", "Quality gates passed"),
    "warn": ("⚠️", "Quality gates passed with warnings"),
    "fail": ("❌", "Quality gates failed"),
    "error": ("🚨", "Evaluation could not be completed"),
}

MAX_METRIC_ROWS = 40
MAX_REGRESSIONS = 15
MAX_FAILURES = 10


def render(report: dict[str, Any], *, run_url: str | None = None) -> str:
    """Build the comment body. Always returns something postable."""
    verdict = str(report.get("verdict", "error"))
    icon, headline = VERDICT_HEADLINE.get(verdict, VERDICT_HEADLINE["error"])

    parts: list[str] = [MARKER, "", f"### {icon} Proofstep — {headline}", ""]
    parts.extend(_summary(report))

    if aborted := report.get("aborted_reason"):
        parts += ["", f"> **Run aborted.** {aborted}", ""]

    parts.extend(_blocking(report))
    parts.extend(_metrics(report))
    parts.extend(_regressions(report))
    parts.extend(_failures(report))
    parts.extend(_warnings(report))
    parts.extend(_footer(report, run_url))

    return _fit("\n".join(parts))


def render_error(message: str, *, suite: str | None = None, run_url: str | None = None) -> str:
    """A comment for a run that never produced a report.

    Posting something is essential: an absent comment reads as "no problems found",
    which is the opposite of what happened.
    """
    parts = [
        MARKER,
        "",
        "### 🚨 Proofstep — evaluation did not run",
        "",
        f"The evaluation{f' for `{suite}`' if suite else ''} failed before producing a report.",
        "",
        "```",
        message.strip()[:3000],
        "```",
        "",
        "_No quality gates were evaluated, so this result says nothing about the change "
        "itself — only that the evaluation could not complete._",
    ]
    if run_url:
        parts += ["", f"[View workflow run]({run_url})"]
    return "\n".join(parts)


# ----------------------------------------------------------------------- sections


def _summary(report: dict[str, Any]) -> list[str]:
    totals = report.get("totals", {})
    dataset = report.get("dataset", {})
    gates = report.get("gates", [])

    blocking = sum(1 for g in gates if g.get("verdict") in ("fail", "error") and g.get("blocking"))
    warnings = sum(
        1 for g in gates if g.get("verdict") in ("fail", "error") and not g.get("blocking")
    )
    passed = sum(1 for g in gates if g.get("verdict") == "pass")

    rows = [
        f"| Suite | `{report.get('suite', '?')}` |",
        f"| Gates | {passed} passed, {blocking} blocking, {warnings} warning |",
        f"| Examples | {totals.get('examples', 0)} ({totals.get('errors', 0)} failed) |",
    ]
    if dataset.get("content_hash"):
        label = dataset.get("name") or "dataset"
        if dataset.get("version"):
            label = f"{label}@{dataset['version']}"
        rows.append(f"| Dataset | `{label}` · `{dataset['content_hash'][:12]}` |")
    if (cost := totals.get("total_cost")) is not None:
        rows.append(f"| Cost | ${float(cost):.4f} |")
    if commit := (report.get("git") or {}).get("commit"):
        rows.append(f"| Commit | `{commit[:8]}` |")

    return ["| | |", "|---|---|", *rows]


def _blocking(report: dict[str, Any]) -> list[str]:
    """The reason the build failed, above the fold and never collapsed."""
    failures = [
        g
        for g in report.get("gates", [])
        if g.get("verdict") in ("fail", "error") and g.get("blocking")
    ]
    if not failures:
        return []

    lines = ["", "#### Blocking failures", ""]
    for gate in failures:
        key = _metric_label(gate)
        lines.append(f"- **`{key}`** — {gate.get('message', 'failed')}")
    return lines


def _metrics(report: dict[str, Any]) -> list[str]:
    metrics = report.get("metrics", [])
    if not metrics:
        return []

    gated = {(g.get("metric_key"), _slice_text(g.get("slice"))) for g in report.get("gates", [])}
    interesting = [
        m
        for m in metrics
        if m.get("slice") is None or (m.get("key"), _slice_text(m.get("slice"))) in gated
    ]
    hidden = len(metrics) - len(interesting)
    shown = interesting[:MAX_METRIC_ROWS]

    lines = [
        "",
        "<details><summary>Metrics</summary>",
        "",
        "| Metric | Baseline | Candidate | Δ | Gate |",
        "|---|---:|---:|---:|---|",
    ]
    gates_by_key = {
        (g.get("metric_key"), _slice_text(g.get("slice"))): g for g in report.get("gates", [])
    }
    for metric in sorted(shown, key=_metric_label):
        gate = gates_by_key.get((metric.get("key"), _slice_text(metric.get("slice"))))
        lines.append(
            "| `{key}` | {baseline} | {candidate} | {delta} | {gate} |".format(
                key=_metric_label(metric),
                baseline=_number(metric.get("baseline")),
                candidate=_number(metric.get("value")),
                delta=_delta(metric.get("absolute_delta")),
                gate=_gate_cell(gate),
            )
        )

    trailing = []
    if hidden or len(interesting) > MAX_METRIC_ROWS:
        folded = hidden + max(0, len(interesting) - MAX_METRIC_ROWS)
        trailing = ["", f"_{folded} further metric(s) in the JSON artifact._"]

    return [*lines, *trailing, "", "</details>"]


def _regressions(report: dict[str, Any]) -> list[str]:
    regressions = report.get("regressed_examples", [])
    if not regressions:
        return []

    lines = [
        "",
        f"<details><summary>Regressed examples ({len(regressions)})</summary>",
        "",
        "| Example | Metric | Baseline | Candidate |",
        "|---|---|---:|---:|",
    ]
    for entry in regressions[:MAX_REGRESSIONS]:
        lines.append(
            f"| `{entry.get('example_id')}` | `{entry.get('metric')}` | "
            f"{_number(entry.get('baseline_score'))} | {_number(entry.get('candidate_score'))} |"
        )
    if len(regressions) > MAX_REGRESSIONS:
        lines += ["", f"_{len(regressions) - MAX_REGRESSIONS} more in the JSON artifact._"]
    return [*lines, "", "</details>"]


def _failures(report: dict[str, Any]) -> list[str]:
    failures = report.get("failures", [])
    if not failures:
        return []

    lines = ["", f"<details><summary>Failed examples ({len(failures)})</summary>", "", "```"]
    for entry in failures[:MAX_FAILURES]:
        lines.append(
            f"{entry.get('example_id')}  {entry.get('status')}  {entry.get('error') or ''}"
        )
    if len(failures) > MAX_FAILURES:
        lines.append(f"… {len(failures) - MAX_FAILURES} more")
    return [*lines, "```", "", "</details>"]


def _warnings(report: dict[str, Any]) -> list[str]:
    notes = list(report.get("hints", []))
    notes += list((report.get("baseline") or {}).get("warnings", []))
    if not notes:
        return []
    return ["", "#### Notes", "", *[f"- {note}" for note in notes]]


def _footer(report: dict[str, Any], run_url: str | None) -> list[str]:
    links = []
    if url := report.get("experiment_url"):
        links.append(f"[View experiment]({url})")
    if run_url:
        links.append(f"[Workflow run]({run_url})")

    baseline = report.get("baseline") or {}
    if baseline.get("dataset_match") is False:
        links.append("⚠️ compared against a different dataset")

    footer = ["", "---", ""]
    footer.append(" · ".join(links) if links else "_Run `proofstep eval` locally to reproduce._")
    return footer


# ------------------------------------------------------------------------ helpers


def _fit(body: str) -> str:
    """Keep the comment postable.

    Truncating is not ideal, but a comment that exceeds the limit is rejected
    outright — and no comment is far worse than a shortened one.
    """
    if len(body) <= TRUNCATION_BUDGET:
        return body

    notice = (
        "\n\n---\n\n_This comment was truncated to fit GitHub's size limit. "
        "The complete report is attached as a workflow artifact._\n"
    )
    keep = TRUNCATION_BUDGET - len(notice)
    # Cut at a line boundary so the result is not half a table row.
    trimmed = body[:keep].rsplit("\n", 1)[0]
    return trimmed + notice


def _metric_label(entry: dict[str, Any]) -> str:
    key = entry.get("key") or entry.get("metric_key") or "?"
    slice_text = _slice_text(entry.get("slice"))
    return f"{key}[{slice_text}]" if slice_text else str(key)


def _slice_text(slice_: dict[str, str] | None) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(slice_.items())) if slice_ else ""


def _gate_cell(gate: dict[str, Any] | None) -> str:
    if gate is None:
        return ""
    icon = {"pass": "✅", "fail": "❌", "warn": "⚠️", "error": "🚨"}.get(gate.get("verdict", ""), "")
    threshold = gate.get("threshold")
    rule = gate.get("rule")
    detail = ""
    if threshold is not None and rule == "minimum":
        detail = f" min {threshold:g}"
    elif threshold is not None and rule == "maximum":
        detail = f" max {threshold:g}"
    elif threshold is not None and rule and "regression" in rule:
        detail = f" maxΔ {threshold:g}"
    if not gate.get("blocking"):
        detail += " (warn)"
    return f"{icon}{detail}"


def _number(value: Any) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number and (abs(number) >= 1000 or abs(number) < 0.001):
        return f"{number:.4g}"
    return f"{number:.4f}".rstrip("0").rstrip(".") or "0"


def _delta(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.4g}"
