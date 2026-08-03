"""The pull-request comment.

Tested as a pure function of a report dict, with no GitHub involved. The properties
that matter are: it always renders something postable, the blocking reason is visible
without expanding anything, and it never exceeds GitHub's size limit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from evalforge_cli.main import app
from evalforge_cli.render.markdown import (
    GITHUB_COMMENT_LIMIT,
    MARKER,
    TRUNCATION_BUDGET,
    render,
    render_error,
)

RUNNER = CliRunner()
ROOT = Path(__file__).resolve().parents[3]


def report(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "report_version": 1,
        "suite": "reply-intent",
        "verdict": "pass",
        "exit_code": 0,
        "aborted_reason": None,
        "git": {"commit": "abcdef1234567890", "branch": "feature"},
        "dataset": {
            "name": "reply-intent",
            "version": "v3",
            "content_hash": "c" * 64,
            "example_count": 6,
        },
        "baseline": {"run_id": None, "dataset_match": True, "warnings": []},
        "totals": {"examples": 6, "errors": 0, "duration_s": 1.2, "total_cost": 0.0031},
        "metrics": [
            {"key": "valid_schema", "slice": None, "value": 1.0, "count": 6, "error_count": 0},
            {"key": "accuracy", "slice": None, "value": 0.9, "count": 6, "error_count": 0},
        ],
        "gates": [
            {
                "metric_key": "valid_schema",
                "slice": None,
                "verdict": "pass",
                "severity": "block",
                "blocking": True,
                "rule": "minimum",
                "threshold": 1.0,
                "actual": 1.0,
                "baseline": None,
                "message": "valid_schema 1",
            }
        ],
        "regressed_examples": [],
        "failures": [],
        "hints": [],
        "experiment_url": None,
    }
    base.update(overrides)
    return base


class TestStructure:
    def test_the_marker_is_first(self) -> None:
        """It is how the Action finds its own comment to edit."""
        assert render(report()).startswith(MARKER)

    def test_a_passing_run_reads_as_passing(self) -> None:
        body = render(report())
        assert "✅" in body
        assert "Quality gates passed" in body

    def test_the_summary_names_the_suite_and_dataset(self) -> None:
        body = render(report())
        assert "`reply-intent`" in body
        assert "cccccccccccc" in body  # truncated content hash
        assert "6 (0 failed)" in body

    def test_the_cost_is_reported(self) -> None:
        assert "$0.0031" in render(report())

    def test_metrics_are_collapsed_but_present(self) -> None:
        """Someone skimming a PR should not have to scroll past a metric table."""
        body = render(report())
        assert "<details><summary>Metrics</summary>" in body
        assert "`accuracy`" in body


class TestBlockingFailures:
    def test_the_blocking_reason_is_above_the_fold(self) -> None:
        """Never inside <details>: it is the reason the build is red."""
        body = render(
            report(
                verdict="fail",
                exit_code=1,
                gates=[
                    {
                        "metric_key": "classes_recall",
                        "slice": {"class": "unsubscribe"},
                        "verdict": "fail",
                        "severity": "block",
                        "blocking": True,
                        "rule": "minimum",
                        "threshold": 0.98,
                        "actual": 0.0,
                        "baseline": None,
                        "message": "classes_recall[class=unsubscribe] 0 < minimum 0.98",
                    }
                ],
            )
        )
        headline, _, rest = body.partition("<details>")
        assert "Blocking failures" in headline
        assert "classes_recall[class=unsubscribe]" in headline
        assert "< minimum 0.98" in headline
        assert rest  # there is still collapsed detail after it

    def test_a_warning_gate_is_not_listed_as_blocking(self) -> None:
        body = render(
            report(
                verdict="warn",
                gates=[
                    {
                        "metric_key": "cost_per_example",
                        "slice": None,
                        "verdict": "fail",
                        "severity": "warn",
                        "blocking": False,
                        "rule": "maximum",
                        "threshold": 0.01,
                        "actual": 0.02,
                        "baseline": None,
                        "message": "cost too high",
                    }
                ],
            )
        )
        assert "Blocking failures" not in body
        assert "⚠️" in body

    def test_the_gate_cell_carries_the_threshold(self) -> None:
        """So the reader never has to open the suite YAML."""
        assert "min 1" in render(report())


class TestAbortedRuns:
    def test_an_aborted_run_says_so_prominently(self) -> None:
        body = render(
            report(
                verdict="error",
                exit_code=2,
                aborted_reason="4 of 6 examples failed (66.7% > max_error_rate 10.0%)",
            )
        )
        assert "Run aborted" in body
        assert "66.7%" in body
        assert "🚨" in body

    def test_render_error_explains_that_nothing_was_measured(self) -> None:
        """An absent comment reads as 'no problems found'."""
        body = render_error("ModuleNotFoundError: no module named 'mypkg'", suite="s.yaml")
        assert body.startswith(MARKER)
        assert "did not run" in body
        assert "says nothing about the change itself" in body
        assert "mypkg" in body


class TestRegressionsAndFailures:
    def test_regressed_examples_are_listed_with_numbers(self) -> None:
        body = render(
            report(
                regressed_examples=[
                    {
                        "example_id": "r-002",
                        "metric": "accuracy",
                        "baseline_score": 1.0,
                        "candidate_score": 0.0,
                        "trace_id": None,
                    }
                ]
            )
        )
        assert "Regressed examples (1)" in body
        assert "`r-002`" in body

    def test_long_regression_lists_are_capped_and_say_so(self) -> None:
        body = render(
            report(
                regressed_examples=[
                    {
                        "example_id": f"r-{i}",
                        "metric": "m",
                        "baseline_score": 1.0,
                        "candidate_score": 0.0,
                    }
                    for i in range(40)
                ]
            )
        )
        assert "Regressed examples (40)" in body
        assert "more in the JSON artifact" in body

    def test_failed_examples_are_listed(self) -> None:
        body = render(
            report(
                totals={"examples": 6, "errors": 1, "duration_s": 1.0, "total_cost": 0},
                failures=[{"example_id": "r-004", "status": "error", "error": "boom"}],
            )
        )
        assert "Failed examples (1)" in body
        assert "boom" in body


class TestNotes:
    def test_hints_are_surfaced(self) -> None:
        body = render(report(hints=["3/4 evaluators are LLM judges."]))
        assert "Notes" in body
        assert "LLM judges" in body

    def test_a_dataset_mismatch_is_flagged_in_the_footer(self) -> None:
        """Comparing across different data yields a confidently wrong conclusion."""
        body = render(report(baseline={"dataset_match": False, "warnings": []}))
        assert "different dataset" in body

    def test_baseline_warnings_appear(self) -> None:
        body = render(report(baseline={"dataset_match": True, "warnings": ["hash differs"]}))
        assert "hash differs" in body


class TestSizeLimit:
    """GitHub rejects a comment over 65,536 characters, so there would be no comment.

    Most sections are capped, so overflow comes from the uncapped one: blocking
    failures. Every blocking gate is listed on purpose — omitting one would hide a
    reason the build is red — which means a suite with hundreds of gates is the
    realistic way to exceed the limit.
    """

    @staticmethod
    def _many_blocking_gates(count: int) -> list[dict[str, Any]]:
        return [
            {
                "metric_key": f"metric_number_{i}_with_a_deliberately_long_name",
                "slice": {"class": f"category_{i}"},
                "verdict": "fail",
                "severity": "block",
                "blocking": True,
                "rule": "minimum",
                "threshold": 0.98,
                "actual": 0.1,
                "baseline": None,
                "message": (
                    f"metric_number_{i} 0.1 < minimum 0.98 — " + "explanatory detail " * 20
                ),
            }
            for i in range(count)
        ]

    def test_a_normal_report_is_well_under_the_limit(self) -> None:
        assert len(render(report())) < 4000

    def test_an_oversized_report_is_truncated_rather_than_rejected(self) -> None:
        body = render(report(verdict="fail", exit_code=1, gates=self._many_blocking_gates(400)))
        assert len(body) <= TRUNCATION_BUDGET
        assert len(body) <= GITHUB_COMMENT_LIMIT

    def test_truncation_is_announced_not_silent(self) -> None:
        body = render(report(verdict="fail", exit_code=1, gates=self._many_blocking_gates(400)))
        assert "truncated" in body
        assert "workflow artifact" in body

    def test_truncation_keeps_the_headline_and_the_first_failures(self) -> None:
        """What survives matters: the verdict and the worst news come first."""
        body = render(report(verdict="fail", exit_code=1, gates=self._many_blocking_gates(400)))
        assert body.startswith(MARKER)
        assert "Quality gates failed" in body
        assert "metric_number_0" in body

    def test_truncation_cuts_at_a_line_boundary(self) -> None:
        """A cut mid-line renders as broken markdown."""
        body = render(report(verdict="fail", exit_code=1, gates=self._many_blocking_gates(400)))
        bullets = [line for line in body.splitlines() if line.startswith("- **`metric_number_")]
        assert bullets

        # Each surviving bullet is whole: it closes its code span and carries its
        # message. A cut mid-bullet would leave an unterminated backtick, which
        # swallows the rest of the comment when GitHub renders it.
        assert all("`** — " in line for line in bullets)
        assert all(line.count("`") % 2 == 0 for line in bullets)


class TestCliCommands:
    SUITE = ROOT / "evals" / "suites" / "reply-intent.yaml"

    @pytest.fixture(autouse=True)
    def _plain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.delenv("EXAMPLE_BREAK_UNSUBSCRIBE", raising=False)
        monkeypatch.chdir(ROOT)

    def test_comment_renders_from_a_real_report(self, tmp_path: Path) -> None:
        target = tmp_path / "r.json"
        RUNNER.invoke(app, ["eval", str(self.SUITE), "-o", str(target)])

        result = RUNNER.invoke(app, ["comment", str(target)])
        assert result.exit_code == 0
        assert MARKER in result.output
        assert "reply-intent" in result.output

    def test_comment_writes_to_a_file(self, tmp_path: Path) -> None:
        target = tmp_path / "r.json"
        RUNNER.invoke(app, ["eval", str(self.SUITE), "-o", str(target)])

        body = tmp_path / "body.md"
        result = RUNNER.invoke(app, ["comment", str(target), "-o", str(body)])
        assert result.exit_code == 0
        assert MARKER in body.read_text(encoding="utf-8")

    def test_comment_includes_the_run_url(self, tmp_path: Path) -> None:
        target = tmp_path / "r.json"
        RUNNER.invoke(app, ["eval", str(self.SUITE), "-o", str(target)])
        result = RUNNER.invoke(
            app, ["comment", str(target), "--run-url", "https://example.test/run/1"]
        )
        assert "https://example.test/run/1" in result.output

    def test_a_missing_report_exits_three(self) -> None:
        assert RUNNER.invoke(app, ["comment", "nope.json"]).exit_code == 3

    def test_a_corrupt_report_exits_three(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        result = RUNNER.invoke(app, ["comment", str(broken)])
        assert result.exit_code == 3
        assert "not valid JSON" in result.output

    def test_comment_error_renders_without_a_report(self) -> None:
        result = RUNNER.invoke(app, ["comment-error", "the runner exploded", "--suite", "s.yaml"])
        assert result.exit_code == 0
        assert "did not run" in result.output

    def test_a_failing_run_produces_a_red_comment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end shape the Action posts on a regression."""
        monkeypatch.setenv("EXAMPLE_BREAK_UNSUBSCRIBE", "1")
        target = tmp_path / "r.json"
        RUNNER.invoke(app, ["eval", str(self.SUITE), "-o", str(target)])

        payload = json.loads(target.read_text(encoding="utf-8"))
        body = render(payload)

        assert "❌" in body
        assert "Quality gates failed" in body
        assert "classes_recall[class=unsubscribe]" in body
        assert "Blocking failures" in body
