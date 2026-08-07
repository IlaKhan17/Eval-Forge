"""The CLI as a user runs it: exit codes, the report contract, terminal output.

The exit code is the whole product from CI's point of view, so it gets a matrix
rather than a happy-path test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalforge_cli.main import app
from evalforge_cli.render.report import REPORT_VERSION, ReportError, validate_report

RUNNER = CliRunner()
ROOT = Path(__file__).resolve().parents[3]
SUITE = ROOT / "evals" / "suites" / "reply-intent.yaml"

#: Examples in the suite's fixture. Named rather than repeated as a literal, because the fixture
#: grew — from six to forty, so that breaking the rare class is a *hidden* regression rather than
#: one the aggregate also catches — and four separate tests asserted the old number.
EXAMPLES = 40


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic output: no colour, and a stable working directory."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("EXAMPLE_BREAK_UNSUBSCRIBE", raising=False)
    monkeypatch.chdir(ROOT)


class TestExitCodes:
    def test_a_passing_suite_exits_zero(self, tmp_path: Path) -> None:
        result = RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(tmp_path / "r.json")])
        assert result.exit_code == 0, result.output

    def test_a_blocking_gate_failure_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The protected metric doing its job: unsubscribe recall collapses."""
        monkeypatch.setenv("EXAMPLE_BREAK_UNSUBSCRIBE", "1")
        result = RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(tmp_path / "r.json")])
        assert result.exit_code == 1, result.output
        assert "classes_recall" in result.output

    def test_a_missing_suite_exits_three(self) -> None:
        """Configuration errors are 3, not 1: 'your suite is broken' and 'your
        change is worse' need different responses."""
        result = RUNNER.invoke(app, ["eval", "does-not-exist.yaml"])
        assert result.exit_code == 3

    def test_an_invalid_suite_exits_three(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: x\n", encoding="utf-8")
        result = RUNNER.invoke(app, ["eval", str(bad)])
        assert result.exit_code == 3

    def test_a_gate_on_an_unknown_metric_exits_three_not_zero(self, tmp_path: Path) -> None:
        """A typo'd gate must never look like a pass."""
        (tmp_path / "d.jsonl").write_text('{"id":"a","input":{}}\n', encoding="utf-8")
        suite = tmp_path / "s.yaml"
        suite.write_text(
            "apiVersion: evalforge.dev/v1\nkind: EvalSuite\nname: s\n"
            "dataset:\n  path: d.jsonl\ntask:\n  entrypoint: m:r\n"
            "evaluators:\n  - name: acc\n    type: exact_match\n"
            "gates:\n  typo:\n    minimum: 0.9\n",
            encoding="utf-8",
        )
        result = RUNNER.invoke(app, ["eval", str(suite)])
        assert result.exit_code == 3
        assert "matches no evaluator" in result.output

    def test_an_unimportable_task_exits_three(self, tmp_path: Path) -> None:
        (tmp_path / "d.jsonl").write_text('{"id":"a","input":{}}\n', encoding="utf-8")
        suite = tmp_path / "s.yaml"
        suite.write_text(
            "apiVersion: evalforge.dev/v1\nkind: EvalSuite\nname: s\n"
            "dataset:\n  path: d.jsonl\ntask:\n  entrypoint: nosuchmodule:run\n"
            "evaluators:\n  - name: acc\n    type: exact_match\n",
            encoding="utf-8",
        )
        result = RUNNER.invoke(app, ["eval", str(suite)])
        assert result.exit_code == 3
        assert "cannot import module" in result.output


class TestDryRun:
    def test_it_makes_no_model_calls_and_exits_zero(self) -> None:
        """Being able to check the wiring for free is the point."""
        result = RUNNER.invoke(app, ["eval", str(SUITE), "--dry-run"])
        assert result.exit_code == 0
        assert "no model calls were made" in result.output

    def test_it_reports_the_plan(self) -> None:
        result = RUNNER.invoke(app, ["eval", str(SUITE), "--dry-run"])
        assert f"{EXAMPLES} examples" in result.output
        assert "judge calls      0" in result.output

    def test_it_writes_no_report(self, tmp_path: Path) -> None:
        target = tmp_path / "r.json"
        RUNNER.invoke(app, ["eval", str(SUITE), "--dry-run", "-o", str(target)])
        assert not target.exists()

    def test_it_still_catches_a_broken_suite(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("apiVersion: evalforge.dev/v1\nname: x\n", encoding="utf-8")
        assert RUNNER.invoke(app, ["eval", str(bad), "--dry-run"]).exit_code == 3


class TestReport:
    def test_the_report_is_written_and_valid(self, tmp_path: Path) -> None:
        target = tmp_path / "report.json"
        RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(target)])

        payload = json.loads(target.read_text(encoding="utf-8"))
        validate_report(payload)  # raises if the contract is broken
        assert payload["report_version"] == REPORT_VERSION
        assert payload["suite"] == "reply-intent"
        assert payload["verdict"] == "pass"
        assert payload["exit_code"] == 0

    def test_the_report_records_the_dataset_hash(self, tmp_path: Path) -> None:
        """Reproducibility: the report must say what data produced it."""
        target = tmp_path / "report.json"
        RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(target)])
        dataset = json.loads(target.read_text(encoding="utf-8"))["dataset"]
        assert len(dataset["content_hash"]) == 64
        assert dataset["example_count"] == EXAMPLES

    def test_error_counts_are_reported_separately_from_counts(self, tmp_path: Path) -> None:
        """An errored evaluation is not a score of zero, all the way to the report."""
        target = tmp_path / "report.json"
        RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(target)])
        metrics = json.loads(target.read_text(encoding="utf-8"))["metrics"]
        assert all("error_count" in m for m in metrics)

    def test_a_failing_run_records_the_blocking_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXAMPLE_BREAK_UNSUBSCRIBE", "1")
        target = tmp_path / "report.json"
        RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(target)])

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["verdict"] == "fail"
        assert payload["exit_code"] == 1
        blocking = [g for g in payload["gates"] if g["verdict"] == "fail" and g["blocking"]]
        assert any(g["metric_key"] == "classes_recall" for g in blocking)

    def test_a_verdict_and_exit_code_that_disagree_are_refused(self) -> None:
        """The Action parses this file; a self-inconsistent report is worse than none."""
        with pytest.raises(ReportError, match="implies exit code"):
            validate_report(
                {
                    "report_version": REPORT_VERSION,
                    "suite": "s",
                    "verdict": "fail",
                    "exit_code": 0,
                    "dataset": {},
                    "metrics": [],
                    "gates": [],
                    "totals": {},
                }
            )

    def test_a_missing_required_field_is_refused(self) -> None:
        with pytest.raises(ReportError, match="missing required field"):
            validate_report({"report_version": REPORT_VERSION, "suite": "s"})

    def test_an_unknown_verdict_is_refused(self) -> None:
        with pytest.raises(ReportError, match="unknown verdict"):
            validate_report(
                {
                    "report_version": REPORT_VERSION,
                    "suite": "s",
                    "verdict": "maybe",
                    "exit_code": 0,
                    "dataset": {},
                    "metrics": [],
                    "gates": [],
                    "totals": {},
                }
            )


class TestTerminalOutput:
    def test_the_gate_column_carries_its_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So the reader never has to open the YAML to interpret a failure."""
        monkeypatch.setenv("EXAMPLE_BREAK_UNSUBSCRIBE", "1")
        result = RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(tmp_path / "r.json")])
        assert "min 0.98" in result.output

    def test_failures_name_the_metric_and_the_numbers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXAMPLE_BREAK_UNSUBSCRIBE", "1")
        result = RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(tmp_path / "r.json")])
        assert "blocking failure" in result.output
        assert "< minimum 0.98" in result.output

    def test_sliced_metrics_are_folded_but_counted(self, tmp_path: Path) -> None:
        """Hidden is fine; silently dropped is not."""
        result = RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(tmp_path / "r.json")])
        assert "sliced metric(s) hidden" in result.output

    def test_verbose_shows_them_all(self, tmp_path: Path) -> None:
        result = RUNNER.invoke(
            app, ["eval", str(SUITE), "-o", str(tmp_path / "r.json"), "--verbose"]
        )
        assert "sliced metric(s) hidden" not in result.output
        assert "classes_recall[class=out_of_office]" in result.output

    def test_no_colour_means_no_escape_codes(self, tmp_path: Path) -> None:
        """This output lands in CI logs and bug reports."""
        result = RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(tmp_path / "r.json")])
        assert "\033[" not in result.output

    def test_the_footer_states_the_verdict_and_exit_code(self, tmp_path: Path) -> None:
        result = RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(tmp_path / "r.json")])
        assert "pass (exit 0)" in result.output


class TestOtherCommands:
    def test_validate_accepts_a_good_suite(self) -> None:
        result = RUNNER.invoke(app, ["validate", str(SUITE)])
        assert result.exit_code == 0
        assert "is valid" in result.output

    def test_validate_rejects_a_bad_suite(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("apiVersion: evalforge.dev/v1\nname: x\n", encoding="utf-8")
        assert RUNNER.invoke(app, ["validate", str(bad)]).exit_code == 3

    def test_doctor_reports_the_environment(self) -> None:
        result = RUNNER.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "python" in result.output
        assert "evalforge_core" in result.output

    def test_doctor_never_prints_a_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A diagnostic that prints a key leaks it into every bug report."""
        monkeypatch.setenv("EVALFORGE_API_KEY", "ef_prod_abcd_supersecretvalue")
        result = RUNNER.invoke(app, ["doctor"])
        assert "supersecretvalue" not in result.output
        assert "api key          set" in result.output

    def test_policy_check_validates_a_policy(self) -> None:
        policy = ROOT / "evals" / "policies" / "email-approval.yaml"
        result = RUNNER.invoke(app, ["policy-check", str(policy)])
        assert result.exit_code == 0
        assert "13 rule(s)" in result.output

    def test_policy_check_rejects_a_broken_policy(self, tmp_path: Path) -> None:
        bad = tmp_path / "p.yaml"
        bad.write_text(
            "name: p\nrules:\n  - id: r\n    kind: forbiden_action\n    actions: [x]\n",
            encoding="utf-8",
        )
        result = RUNNER.invoke(app, ["policy-check", str(bad)])
        assert result.exit_code == 3
        assert "Did you mean" in result.output


class TestLimitAndJournal:
    def test_limit_truncates_the_run(self, tmp_path: Path) -> None:
        target = tmp_path / "r.json"
        RUNNER.invoke(app, ["eval", str(SUITE), "-o", str(target), "--limit", "2"])
        assert json.loads(target.read_text(encoding="utf-8"))["totals"]["examples"] == 2

    def test_a_journal_is_written_and_resumable(self, tmp_path: Path) -> None:
        journal = tmp_path / "run.jsonl"
        RUNNER.invoke(
            app, ["eval", str(SUITE), "-o", str(tmp_path / "a.json"), "--journal", str(journal)]
        )
        assert len(journal.read_text(encoding="utf-8").strip().splitlines()) == EXAMPLES

        result = RUNNER.invoke(
            app, ["eval", str(SUITE), "-o", str(tmp_path / "b.json"), "--resume", str(journal)]
        )
        assert result.exit_code == 0
        assert json.loads((tmp_path / "b.json").read_text())["totals"]["examples"] == EXAMPLES
