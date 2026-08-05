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
import json
import os
import platform
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from evalforge_cli import calibration, runner
from evalforge_cli.render import calibration as calibration_render
from evalforge_cli.render import markdown as markdown_module
from evalforge_cli.render import report as report_module
from evalforge_cli.render import terminal
from evalforge_cli.suite.loader import SuiteError, load_suite
from evalforge_core.calibration import check_requirement
from evalforge_core.calibration_runner import report_to_dict
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
    model_client: Annotated[
        str | None,
        typer.Option("--model-client", help="module:factory returning a ModelClient, for judges"),
    ] = None,
) -> None:
    """Run a suite, apply its gates, and exit non-zero on a blocking failure.

    A suite with LLM judges needs `--model-client` (or `EVALFORGE_MODEL_CLIENT`). Provider SDKs
    are deliberately absent from `evaluation-core` so local mode works with no dependencies, so
    the model access is supplied by the project being evaluated.
    """
    try:
        loaded = load_suite(suite_path, overrides=_parse_overrides(set_))
    except SuiteError as exc:
        _fail(str(exc), runner.exit_code_for_setup_error())
        return

    if dry_run:
        _print_plan(loaded)
        raise typer.Exit(0)

    needs_models = any(spec.type == "llm_judge" for spec in loaded.suite.evaluators)
    models = None
    if needs_models:
        try:
            models = calibration.load_model_client(model_client)
        except calibration.CalibrationCommandError as exc:
            # Refused before running anything. A judge with no client returns an errored score
            # on every example, which surfaces as "metric with no measurements" — a confusing
            # way to say "you forgot to pass a model client".
            _fail(str(exc), runner.exit_code_for_setup_error())
            return

    try:
        outcome = asyncio.run(
            runner.execute(loaded, models=models, journal=journal, resume=resume, limit=limit)
        )
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
def comment(
    report_path: Annotated[Path, typer.Argument(help="A report JSON produced by `eval`")],
    run_url: Annotated[
        str | None, typer.Option("--run-url", help="Link back to the CI run")
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write markdown here instead of stdout")
    ] = None,
) -> None:
    """Render a report as pull-request markdown.

    Deliberately knows nothing about GitHub: it reads a file and writes markdown, so
    it is a pure function that can be snapshot-tested, and any CI system can post
    the result however it likes.
    """
    if not report_path.exists():
        _fail(f"report not found: {report_path}", runner.exit_code_for_setup_error())
        return

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"{report_path}: not valid JSON: {exc}", runner.exit_code_for_setup_error())
        return

    body = markdown_module.render(payload, run_url=run_url)
    if output:
        output.write_text(body, encoding="utf-8")
        typer.echo(f"wrote {output}")
    else:
        typer.echo(body)


@app.command(name="comment-error")
def comment_error(
    message: Annotated[str, typer.Argument(help="What went wrong")],
    suite: Annotated[str | None, typer.Option("--suite")] = None,
    run_url: Annotated[str | None, typer.Option("--run-url")] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Render a comment for a run that never produced a report.

    Posting something matters: an absent comment reads as "no problems found",
    which is the opposite of what happened.
    """
    body = markdown_module.render_error(message, suite=suite, run_url=run_url)
    if output:
        output.write_text(body, encoding="utf-8")
        typer.echo(f"wrote {output}")
    else:
        typer.echo(body)


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


@app.command()
def calibrate(  # noqa: PLR0917 — Typer maps CLI options onto arguments
    suite_path: Annotated[Path, typer.Argument(help="Path to the suite YAML")],
    evaluator: Annotated[
        str, typer.Option("--evaluator", "-e", help="Name of the llm_judge to calibrate")
    ],
    labels: Annotated[
        Path | None,
        typer.Option(
            "--labels", help="Labelled JSONL; defaults to the judge's calibration.dataset"
        ),
    ] = None,
    verdicts: Annotated[
        Path | None,
        typer.Option(
            "--verdicts",
            help="Recompute from recorded judge verdicts instead of calling the model",
        ),
    ] = None,
    model_client: Annotated[
        str | None,
        typer.Option("--model-client", help="module:factory returning a ModelClient"),
    ] = None,
    concurrency: Annotated[int, typer.Option("--concurrency", help="Parallel judge calls")] = 4,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report the plan and cost without calling the model")
    ] = False,
    write: Annotated[
        bool, typer.Option("--write/--no-write", help="Store the record for CI to read")
    ] = True,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Also write the raw report JSON here")
    ] = None,
) -> None:
    """Measure a judge against human labels and record the result.

    Exit 0 when the judge meets its requirement, 1 when it does not. Non-zero is
    deliberate: calibration belongs in CI, and "the judge got worse" should be able to
    fail a build the same way a metric regression does.
    """
    try:
        loaded = load_suite(suite_path)
        plan = calibration.plan(loaded, evaluator=evaluator, labels=labels)
    except (SuiteError, calibration.CalibrationCommandError) as exc:
        _fail(str(exc), runner.exit_code_for_setup_error())
        return

    style = terminal.Style(colour=terminal.use_colour(), unicode_=terminal.use_unicode())
    typer.echo(style.paint(f"EvalForge · calibrate {evaluator}", terminal.BOLD))
    typer.echo(f"  labelled set     {plan.labels_path} ({len(plan.cases)} examples)")
    typer.echo(f"  label counts     {plan.label_summary}")
    typer.echo(f"  judge version    {plan.version_hash}")
    typer.echo(f"  judge calls      {plan.judge_calls}")

    if dry_run:
        typer.echo(style.paint("\nno model calls were made", terminal.DIM))
        raise typer.Exit(0)

    try:
        report = calibration.produce(
            plan, verdicts_path=verdicts, model_client=model_client, concurrency=concurrency
        )
    except calibration.CalibrationCommandError as exc:
        _fail(str(exc), runner.exit_code_for_setup_error())
        return
    except KeyboardInterrupt:
        _fail("cancelled", 130)
        return

    check = check_requirement(report, plan.requirement)
    typer.echo("")
    typer.echo(
        calibration_render.render(
            report,
            check,
            evaluator=evaluator,
            version_hash=plan.version_hash,
            style=style,
        )
    )

    payload = report_to_dict(report)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if write:
        stored = calibration.store(plan, report, check)
        typer.echo(f"\nrecorded {stored}")
        typer.echo(
            style.paint(
                "commit this file: it is the evidence CI reads to decide whether the "
                "judge can be trusted",
                terminal.DIM,
            )
        )

    raise typer.Exit(0 if check.satisfied else 1)
