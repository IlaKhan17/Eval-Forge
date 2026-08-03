"""The `evalforge` command.

Exit codes are the contract with CI (docs/EVALUATION_ENGINE.md §7):

    0  pass, or warn-only
    1  a blocking gate failed
    2  execution error — evaluators broke, or too many examples failed
    3  configuration error — the suite itself is wrong
    130 cancelled

Distinguishing 1 from 3 matters: "your change is worse" and "your suite is broken"
call for completely different responses, and collapsing them trains people to
ignore the exit code.
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from evalforge_cli import runner
from evalforge_cli.render import report as report_module
from evalforge_cli.render import terminal
from evalforge_cli.suite.loader import SuiteError, load_suite
from evalforge_trajectory import PolicyError, evaluate_policy, load_policy_file
from evalforge_types import Trace

app = typer.Typer(
    name="evalforge",
    help="Evaluation CI and trajectory testing for AI agents.",
    no_args_is_help=True,
    add_completion=False,
)


def _fail(message: str, code: int) -> None:
    style = terminal.Style(colour=terminal.use_colour(sys.stderr), unicode_=terminal.use_unicode())
    typer.echo(style.paint(f"{style.cross} {message}", terminal.RED), err=True)
    raise typer.Exit(code)


def _parse_overrides(values: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for entry in values or []:
        key, separator, value = entry.partition("=")
        if not separator:
            _fail(f"--set expects key=value, got {entry!r}", runner.exit_code_for_setup_error())
        overrides[key.strip()] = value
    return overrides


@app.command()
def eval(  # noqa: PLR0917 — Typer maps CLI options onto arguments
    suite_path: Annotated[Path, typer.Argument(help="Path to the suite YAML")],
    local: Annotated[  # noqa: ARG001 — reserved for remote execution
        bool, typer.Option("--local", help="Run offline; never contact a server")
    ] = True,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and plan without any model calls")
    ] = False,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="JSON report path")] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Only run the first N examples")
    ] = None,
    set_: Annotated[
        list[str] | None, typer.Option("--set", help="Override a suite field: a.b=value")
    ] = None,
    journal: Annotated[
        Path | None, typer.Option("--journal", help="Write a resumable run journal")
    ] = None,
    resume: Annotated[
        Path | None, typer.Option("--resume", help="Skip examples already in this journal")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show every sliced metric")
    ] = False,
) -> None:
    """Run a suite, apply its gates, and exit non-zero on a blocking failure."""
    try:
        loaded = load_suite(suite_path, overrides=_parse_overrides(set_))
    except SuiteError as exc:
        _fail(str(exc), runner.exit_code_for_setup_error())
        return

    if dry_run:
        _print_plan(loaded)
        raise typer.Exit(0)

    try:
        outcome = asyncio.run(runner.execute(loaded, journal=journal, resume=resume, limit=limit))
    except runner.RunError as exc:
        _fail(str(exc), runner.exit_code_for_setup_error())
        return
    except KeyboardInterrupt:
        _fail("cancelled", 130)
        return

    report_path = str(output or loaded.suite.report.output)
    payload = report_module.build_report(
        outcome.result,
        comparison=outcome.comparison,
        git_commit=runner.git_context()[0],
        git_branch=runner.git_context()[1],
        hints=loaded.hints,
    )
    if "json" in loaded.suite.report.formats:
        report_module.write_report(payload, report_path)

    if "terminal" in loaded.suite.report.formats:
        typer.echo(
            terminal.render(
                outcome.result,
                comparison=outcome.comparison,
                hints=loaded.hints,
                report_path=report_path if "json" in loaded.suite.report.formats else None,
                baseline_label=outcome.baseline_label,
                verbose=verbose,
            )
        )

    raise typer.Exit(outcome.exit_code)


def _print_plan(loaded: Any) -> None:
    """What the run would do, and roughly what it would cost.

    A suite can be expensive; being able to check the wiring for free is the point.
    """
    try:
        plan = runner.plan_run(loaded)
    except (runner.RunError, SuiteError) as exc:
        _fail(str(exc), runner.exit_code_for_setup_error())
        return

    style = terminal.Style(colour=terminal.use_colour(), unicode_=terminal.use_unicode())
    typer.echo(style.paint(f"EvalForge · {plan.suite} (dry run)", terminal.BOLD))
    typer.echo(f"  dataset          {plan.dataset} ({plan.example_count} examples)")
    typer.echo(f"  evaluators       {', '.join(plan.evaluator_names) or 'none'}")
    if plan.corpus_names:
        typer.echo(f"  corpus metrics   {', '.join(plan.corpus_names)}")
    typer.echo(f"  gates            {plan.gate_count}")
    typer.echo(f"  baseline         {plan.baseline}")
    typer.echo(
        f"  judge calls      {plan.judge_calls}"
        + ("  (no model calls were made)" if plan.judge_calls else "")
    )
    for hint in plan.hints:
        typer.echo(style.paint(f"  {style.warn} {hint}", terminal.YELLOW))
    typer.echo(style.paint("\nno model calls were made", terminal.DIM))


@app.command()
def validate(
    suite_path: Annotated[Path, typer.Argument(help="Path to the suite YAML")],
) -> None:
    """Check a suite without running it."""
    try:
        loaded = load_suite(suite_path)
    except SuiteError as exc:
        _fail(str(exc), runner.exit_code_for_setup_error())
        return

    style = terminal.Style(colour=terminal.use_colour(), unicode_=terminal.use_unicode())
    typer.echo(style.paint(f"{style.tick} {loaded.path.name} is valid", terminal.GREEN))
    typer.echo(f"  {len(loaded.suite.evaluators)} evaluator(s), {len(loaded.suite.gates)} gate(s)")
    for hint in loaded.hints:
        typer.echo(style.paint(f"  {style.warn} {hint}", terminal.YELLOW))


@app.command()
def doctor() -> None:
    """Report the environment EvalForge sees, for debugging a broken setup."""
    style = terminal.Style(colour=terminal.use_colour(), unicode_=terminal.use_unicode())
    commit, branch, dirty = runner.git_context()

    typer.echo(style.paint("EvalForge doctor", terminal.BOLD))
    typer.echo(f"  python           {platform.python_version()} ({sys.executable})")
    typer.echo(f"  cwd              {Path.cwd()}")
    typer.echo(
        f"  git              {branch or '—'} @ {(commit or '—')[:8]}{' (dirty)' if dirty else ''}"
    )
    typer.echo(f"  endpoint         {os.environ.get('EVALFORGE_ENDPOINT', '<unset>')}")
    # Presence only, never the value. A diagnostic command that prints a credential
    # is a diagnostic command that leaks one into a bug report.
    typer.echo(f"  api key          {'set' if os.environ.get('EVALFORGE_API_KEY') else 'unset'}")
    typer.echo(f"  colour output    {terminal.use_colour()}")
    typer.echo(f"  unicode output   {terminal.use_unicode()}")

    for package in ("evalforge_types", "evalforge_core", "evalforge_trajectory", "evalforge"):
        try:
            module = __import__(package)
        except ImportError:
            typer.echo(style.paint(f"  {style.cross} {package} not importable", terminal.RED))
        else:
            typer.echo(f"  {package:<20} {getattr(module, '__version__', 'unknown')}")


@app.command(name="policy-check")
def policy_check(
    policy_path: Annotated[Path, typer.Argument(help="Policy YAML")],
    trace_path: Annotated[
        Path | None, typer.Argument(help="A trace JSON file to evaluate against")
    ] = None,
) -> None:
    """Validate a trajectory policy, and optionally evaluate it against a trace."""
    style = terminal.Style(colour=terminal.use_colour(), unicode_=terminal.use_unicode())
    try:
        loaded = load_policy_file(policy_path)
    except PolicyError as exc:
        _fail(str(exc), runner.exit_code_for_setup_error())
        return

    typer.echo(
        style.paint(
            f"{style.tick} {policy_path.name}: {len(loaded.policy.rules)} rule(s)", terminal.GREEN
        )
    )
    if trace_path is None:
        return

    trace = Trace.model_validate_json(trace_path.read_text(encoding="utf-8"))
    result = evaluate_policy(loaded, trace)
    typer.echo(result.format(policy_path=str(policy_path)))
    raise typer.Exit(0 if result.passed else 1)


if __name__ == "__main__":  # pragma: no cover
    app()
