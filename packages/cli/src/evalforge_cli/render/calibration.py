"""Terminal rendering of a calibration report.

Ordered by what a reader has to decide: does this judge pass, why not, and what should
change. κ comes before agreement because agreement alone is the number that misleads —
90 % agreement on a 90 %-majority set means nothing, and putting it first invites exactly
that reading.
"""

from __future__ import annotations

from evalforge_cli.render.terminal import GREEN, RED, YELLOW, Style
from evalforge_core.calibration import CalibrationReport, RequirementCheck


def render(  # noqa: PLR0912 — a report is a sequence of small sections
    report: CalibrationReport,
    check: RequirementCheck,
    *,
    evaluator: str,
    version_hash: str,
    style: Style | None = None,
) -> str:
    theme = style or Style(colour=False, unicode_=True)
    lines: list[str] = []

    verdict = "pass" if check.satisfied else "fail"
    lines.append(
        f"{theme.mark(verdict)} calibration {evaluator} "
        f"{theme.paint(version_hash, YELLOW if not check.satisfied else GREEN)}"
    )
    lines.append("")

    kappa = _kappa_line(report)
    lines.append(f"  {'κ':<22} {kappa}")
    lines.append(
        f"  {'agreement':<22} {report.agreement:.3f}  "
        f"[{report.agreement_ci[0]:.3f}, {report.agreement_ci[1]:.3f}]"
    )
    lines.append(f"  {'examples':<22} {report.n_examples}")
    if report.n_errored:
        lines.append(
            f"  {'errored calls':<22} "
            f"{theme.paint(f'{report.n_errored} ({report.error_rate:.1%})', YELLOW)}"
        )

    # The directional rates, always both, always labelled with what they mean. Reporting
    # a single "error rate" here would hide the only distinction that matters.
    lines.append(
        f"  {'false pass':<22} {_rate(report.false_pass_rate)}"
        "   (judge passed what a human failed — ships defects)"
    )
    lines.append(
        f"  {'false fail':<22} {_rate(report.false_fail_rate)}"
        "   (judge failed what a human passed — erodes trust)"
    )

    if report.human_kappa is not None:
        ceiling = f"{report.human_kappa:.3f} on {report.n_ceiling_examples} doubly-labelled"
        if report.at_human_ceiling:
            ceiling += theme.paint("  — judge is at the ceiling", GREEN)
        lines.append(f"  {'human ceiling κ':<22} {ceiling}")

    if report.leniency is not None:
        lines.append(f"  {'leniency':<22} {report.leniency:+.2f} scale points vs humans")
    if report.scale_compression is not None:
        lines.append(f"  {'scale used':<22} {report.scale_compression:.0%} of the human spread")
    if report.verbosity_bias is not None:
        lines.append(f"  {'verbosity bias':<22} {report.verbosity_bias:+.2f}")

    if report.position_bias is not None:
        bias = report.position_bias
        text = (
            f"{bias.inconsistency_rate:.1%} inconsistent, "
            f"{bias.first_position_rate:.1%} first-position"
        )
        lines.append(f"  {'order effects':<22} {theme.paint(text, RED) if bias.biased else text}")

    lines.append(f"  {'cost':<22} {report.total_cost} total, {report.mean_cost} per example")
    lines.append(
        f"  {'latency':<22} p50 {report.p50_latency_ms:.0f}ms  p95 {report.p95_latency_ms:.0f}ms"
    )

    if report.per_class:
        lines.append("")
        lines.append("  per class")
        lines.append(f"    {'label':<20} {'n':>5} {'recall':>8} {'precision':>10}  confused with")
        for entry in report.per_class:
            confusion = (
                f"{entry.top_confusion[0]} x{entry.top_confusion[1]}" if entry.top_confusion else ""
            )
            lines.append(
                f"    {entry.label:<20} {entry.support:>5} {entry.recall:>8.3f} "
                f"{entry.precision:>10.3f}  {confusion}"
            )

    if check.failures:
        lines.append("")
        lines.append(theme.paint("  requirement not met", RED))
        for failure in check.failures:
            lines.append(f"    {theme.paint(theme.cross, RED)} {failure}")

    if check.warnings:
        lines.append("")
        for warning in check.warnings:
            lines.append(f"  {theme.paint(theme.warn, YELLOW)} {warning}")

    if report.notes:
        lines.append("")
        for note in report.notes:
            lines.append(f"  {theme.paint('note', YELLOW)} {note}")

    return "\n".join(lines)


def _kappa_line(report: CalibrationReport) -> str:
    if report.kappa is None:
        # Never printed as a number. "κ 0.000" would read as a measured result rather
        # than an undefined one, and the reason is the actionable part.
        return f"undefined — {report.kappa_undefined_reason}"
    interval = f"  [{report.kappa_ci[0]:.3f}, {report.kappa_ci[1]:.3f}]" if report.kappa_ci else ""
    return f"{report.kappa:.3f}{interval}  ({report.kappa_kind})"


def _rate(value: float | None) -> str:
    # "unmeasured" rather than 0.000: a rate over an empty denominator is not zero, and a
    # calibration set with no negatives cannot show whether the judge catches anything.
    return "unmeasured" if value is None else f"{value:.3f}"
