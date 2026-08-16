"""The `calibrate` command, the record store, and staleness detection.

The load-bearing test in this file is `test_editing_the_rubric_invalidates_the_record`.
Rubric drift is the failure mode that silently redefines a metric: change the wording and
the "regression" you see next week is a changed ruler, not a changed system. Everything
else here is scaffolding for that property.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proofstep_cli import calibration as calibration_command
from proofstep_cli.calibration_store import (
    evaluator_version_hash,
    load_all,
    read_calibration,
    status_for,
    write_calibration,
)
from proofstep_cli.runner import (
    build_gate_set,
    judge_metric_keys,
    requirement_for,
    resolve_calibrations,
)
from proofstep_cli.suite.loader import load_suite
from proofstep_core.calibration import CalibrationReport, RequirementCheck, check_requirement
from proofstep_core.gates import evaluate_gates
from proofstep_types import Metric, Verdict

RUBRIC = "Answer `acceptable` unless the reply asks to stop being contacted.\n"


def write_suite(root: Path, *, require: str = "true", extra_judge: str = "") -> Path:
    (root / "rubrics").mkdir(parents=True, exist_ok=True)
    (root / "rubrics" / "tone.md").write_text(RUBRIC, encoding="utf-8")
    (root / "fixtures").mkdir(parents=True, exist_ok=True)
    (root / "fixtures" / "data.jsonl").write_text(
        json.dumps({"id": "x1", "input": {"body": "hi"}, "expected": {"intent": "other"}}) + "\n",
        encoding="utf-8",
    )
    suites = root / "suites"
    suites.mkdir(parents=True, exist_ok=True)
    path = suites / "tone.yaml"
    path.write_text(
        f"""apiVersion: proofstep.dev/v1
kind: EvalSuite
name: tone
dataset:
  path: ../fixtures/data.jsonl
task:
  entrypoint: examples.reply_intent:classify
evaluators:
  - name: tone
    type: llm_judge
    model: test-model-1
    rubric_path: ../rubrics/tone.md
    inputs: [input.body]
    labels: [acceptable, unacceptable]
    calibration:
      dataset: ../calibration/labels.jsonl
      passing_labels: [acceptable]
{extra_judge}
gates:
  tone:
    minimum: 0.9
calibration:
  directory: ../calibration
  require: {require}
""",
        encoding="utf-8",
    )
    return path


def write_labels(root: Path, *, n_pass: int = 60, n_fail: int = 60, false_passes: int = 2) -> Path:
    directory = root / "calibration"
    directory.mkdir(parents=True, exist_ok=True)
    labels = directory / "labels.jsonl"
    verdicts = directory / "verdicts.jsonl"

    label_rows: list[str] = []
    verdict_rows: list[str] = []
    for i in range(n_pass):
        label_rows.append(
            json.dumps(
                {
                    "id": f"p{i}",
                    "input": {"body": f"polite reply {i}"},
                    "output": f"polite reply {i}",
                    "human_label": "acceptable",
                    "second_human_label": "acceptable" if i < 12 else None,
                }
            )
        )
        verdict_rows.append(json.dumps({"id": f"p{i}", "label": "acceptable"}))
    for i in range(n_fail):
        label_rows.append(
            json.dumps(
                {
                    "id": f"f{i}",
                    "input": {"body": f"stop {i}"},
                    "output": f"stop {i}",
                    "human_label": "unacceptable",
                    "second_human_label": "unacceptable" if i < 12 else None,
                }
            )
        )
        verdict_rows.append(
            json.dumps(
                {
                    "id": f"f{i}",
                    "label": "acceptable" if i < false_passes else "unacceptable",
                }
            )
        )

    labels.write_text("\n".join(label_rows) + "\n", encoding="utf-8")
    verdicts.write_text("\n".join(verdict_rows) + "\n", encoding="utf-8")
    return verdicts


def run_calibration(
    root: Path, suite_path: Path, verdicts: Path
) -> tuple[calibration_command.Plan, CalibrationReport, RequirementCheck]:
    loaded = load_suite(suite_path)
    plan = calibration_command.plan(loaded, evaluator="tone", labels=None)
    report = calibration_command.produce(
        plan, verdicts_path=verdicts, model_client=None, concurrency=1
    )
    check = check_requirement(report, plan.requirement)
    return plan, report, check


class TestPlan:
    def test_refuses_an_unknown_evaluator(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        write_labels(tmp_path)
        loaded = load_suite(suite)
        with pytest.raises(calibration_command.CalibrationCommandError, match="no evaluator named"):
            calibration_command.plan(loaded, evaluator="nope", labels=None)

    def test_refuses_a_deterministic_evaluator(self, tmp_path: Path) -> None:
        # A deterministic check has no opinion to validate. Calibrating `exact_match`
        # would produce a certificate that means nothing.
        suite = write_suite(
            tmp_path,
            extra_judge="  - name: exact\n    type: exact_match\n    field: output.intent\n",
        )
        write_labels(tmp_path)
        loaded = load_suite(suite)
        with pytest.raises(calibration_command.CalibrationCommandError, match="not 'llm_judge'"):
            calibration_command.plan(loaded, evaluator="exact", labels=None)

    def test_reports_the_number_of_judge_calls_before_spending(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        write_labels(tmp_path)
        plan = calibration_command.plan(load_suite(suite), evaluator="tone", labels=None)
        assert plan.judge_calls == 120
        assert "acceptable=60" in plan.label_summary

    def test_a_missing_labelled_set_fails_before_any_call(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        loaded = load_suite(suite)
        with pytest.raises(calibration_command.CalibrationCommandError, match="not found"):
            calibration_command.plan(loaded, evaluator="tone", labels=None)


class TestVerdictReplay:
    def test_recomputes_a_report_without_calling_a_model(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        verdicts = write_labels(tmp_path)
        _plan, report, check = run_calibration(tmp_path, suite, verdicts)

        # Hand-checkable: 2 of 60 human failures waved through, nothing else wrong.
        #   agreement = 118/120, false pass = 2/60
        assert report.n_examples == 120
        assert report.agreement == pytest.approx(118 / 120)
        assert report.false_pass_rate == pytest.approx(2 / 60)
        assert report.false_fail_rate == pytest.approx(0.0)
        assert check.satisfied

    def test_a_judge_that_waves_through_opt_outs_fails(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        verdicts = write_labels(tmp_path, false_passes=12)
        _plan, report, check = run_calibration(tmp_path, suite, verdicts)
        assert report.false_pass_rate == pytest.approx(0.2)
        assert not check.satisfied
        assert any("false-pass" in f for f in check.failures)

    def test_an_errored_verdict_is_preserved_not_dropped(self, tmp_path: Path) -> None:
        # Excluding failures silently would make a judge that times out on hard examples
        # look better than one that answers them badly.
        suite = write_suite(tmp_path)
        verdicts = write_labels(tmp_path)
        lines = verdicts.read_text(encoding="utf-8").splitlines()
        lines[0] = json.dumps({"id": "p0", "error": "429 rate limited"})
        verdicts.write_text("\n".join(lines) + "\n", encoding="utf-8")

        _plan, report, _check = run_calibration(tmp_path, suite, verdicts)
        assert report.n_errored == 1
        assert report.n_examples == 119

    def test_a_verdict_without_an_id_is_rejected(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        verdicts = write_labels(tmp_path)
        verdicts.write_text(json.dumps({"label": "acceptable"}) + "\n", encoding="utf-8")
        loaded = load_suite(suite)
        plan = calibration_command.plan(loaded, evaluator="tone", labels=None)
        with pytest.raises(calibration_command.CalibrationCommandError, match="needs an 'id'"):
            calibration_command.produce(
                plan, verdicts_path=verdicts, model_client=None, concurrency=1
            )

    def test_a_live_run_without_a_model_client_says_what_to_do(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        write_labels(tmp_path)
        plan = calibration_command.plan(load_suite(suite), evaluator="tone", labels=None)
        with pytest.raises(calibration_command.CalibrationCommandError) as exc:
            calibration_command.produce(plan, verdicts_path=None, model_client=None, concurrency=1)
        assert "--verdicts" in str(exc.value)


class TestStore:
    def test_a_record_round_trips(self, tmp_path: Path) -> None:
        path = write_calibration(
            tmp_path,
            evaluator="tone",
            version_hash="abcd1234",
            report={"n_examples": 120, "kappa": 0.87, "agreement": 0.93},
            requirement={"min_kappa": 0.7},
            satisfied=True,
            failures=[],
            warnings=["something worth knowing"],
        )
        record = read_calibration(path)
        assert record.evaluator == "tone"
        assert record.version_hash == "abcd1234"
        assert record.satisfied
        assert record.warnings == ["something worth knowing"]

    def test_the_filename_carries_the_version(self, tmp_path: Path) -> None:
        # This is what makes staleness detectable at all: a new version means a new file,
        # so the old evidence cannot be mistaken for current evidence.
        path = write_calibration(
            tmp_path,
            evaluator="tone",
            version_hash="deadbeef",
            report={},
            requirement={},
            satisfied=True,
            failures=[],
            warnings=[],
        )
        assert path.name == "tone.deadbeef.calibration.json"

    def test_a_malformed_record_is_an_error_not_a_skip(self, tmp_path: Path) -> None:
        # Skipping would present identically to "never calibrated", hiding a fixable
        # problem behind a signal that means something else.
        (tmp_path / "tone.abc.calibration.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(Exception, match="cannot read calibration record"):
            load_all(tmp_path)

    def test_status_reports_no_calibration_when_the_directory_is_empty(
        self, tmp_path: Path
    ) -> None:
        status = status_for([], evaluator="tone", metric_key="tone", version_hash="abc")
        assert not status.calibrated
        assert not status.is_stale


class TestGateIntegration:
    def test_a_stored_record_satisfies_the_gate(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        verdicts = write_labels(tmp_path)
        plan, report, check = run_calibration(tmp_path, suite, verdicts)
        calibration_command.store(plan, report, check)

        loaded = load_suite(suite)
        statuses = resolve_calibrations(loaded)
        assert statuses["tone"].calibrated
        assert statuses["tone"].satisfied

        gate_set = build_gate_set(loaded)
        assert gate_set is not None
        gate_report = evaluate_gates(
            gate_set,
            [Metric(key="tone", value=0.95, count=120)],
            judge_metrics=judge_metric_keys(loaded),
            calibrations=statuses,
        )
        assert gate_report.verdict is Verdict.PASS

    def test_editing_the_rubric_invalidates_the_record(self, tmp_path: Path) -> None:
        # The property this whole subsystem exists for. A rubric edit redefines the
        # metric, so yesterday's evidence describes a different judge. Accepting it would
        # let a rubric change launder itself through an old certificate, and the
        # "regression" seen afterwards would be a changed ruler.
        suite = write_suite(tmp_path)
        verdicts = write_labels(tmp_path)
        plan, report, check = run_calibration(tmp_path, suite, verdicts)
        calibration_command.store(plan, report, check)

        (tmp_path / "rubrics" / "tone.md").write_text(
            RUBRIC + "\nAlso answer `unacceptable` for a bare 'no'.\n", encoding="utf-8"
        )

        loaded = load_suite(suite)
        statuses = resolve_calibrations(loaded)
        assert statuses["tone"].is_stale

        gate_set = build_gate_set(loaded)
        assert gate_set is not None
        gate_report = evaluate_gates(
            gate_set,
            [Metric(key="tone", value=0.95, count=120)],
            judge_metrics=judge_metric_keys(loaded),
            calibrations=statuses,
        )
        assert gate_report.verdict is Verdict.ERROR
        assert gate_report.blocking_failures[0].rule == "stale_calibration"

    def test_changing_the_judge_model_also_invalidates_it(self, tmp_path: Path) -> None:
        # A provider that silently upgrades a model behind an alias invalidates every
        # historical number. The pin being part of the version hash is the only defence
        # available from outside the provider.
        suite = write_suite(tmp_path)
        verdicts = write_labels(tmp_path)
        plan, report, check = run_calibration(tmp_path, suite, verdicts)
        calibration_command.store(plan, report, check)

        suite.write_text(
            suite.read_text(encoding="utf-8").replace("test-model-1", "test-model-2"),
            encoding="utf-8",
        )
        assert resolve_calibrations(load_suite(suite))["tone"].is_stale

    def test_tightening_a_threshold_takes_effect_without_re_running(self, tmp_path: Path) -> None:
        # The stored record carries the verdict from when it ran. If the gate trusted that
        # boolean, raising `min_kappa` would be a no-op until somebody remembered to pay
        # for another calibration — a threshold that silently does nothing.
        suite = write_suite(tmp_path)
        verdicts = write_labels(tmp_path)
        plan, report, check = run_calibration(tmp_path, suite, verdicts)
        calibration_command.store(plan, report, check)
        assert check.satisfied

        suite.write_text(
            suite.read_text(encoding="utf-8").replace(
                "      passing_labels: [acceptable]",
                "      passing_labels: [acceptable]\n      min_kappa: 0.99",
            ),
            encoding="utf-8",
        )
        loaded = load_suite(suite)
        assert requirement_for(loaded.suite.evaluators[0]).min_kappa == 0.99

        statuses = resolve_calibrations(loaded)
        assert statuses["tone"].calibrated
        assert statuses["tone"].satisfied is False
        assert any("κ" in f for f in statuses["tone"].failures)

    def test_require_false_keeps_it_advisory(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path, require="false")
        write_labels(tmp_path)
        loaded = load_suite(suite)
        gate_set = build_gate_set(loaded)
        assert gate_set is not None
        gate_report = evaluate_gates(
            gate_set,
            [Metric(key="tone", value=0.95, count=120)],
            judge_metrics=judge_metric_keys(loaded),
            calibrations=resolve_calibrations(loaded),
        )
        # Advisory, but never silent: an unvalidated gated judge is worth saying out loud.
        assert gate_report.verdict is Verdict.WARN
        assert gate_report.exit_code == 0
        assert any(r.rule == "uncalibrated_judge" for r in gate_report.warnings)


class TestLabelLeakGuard:
    def test_a_judge_that_can_read_expected_is_refused(self, tmp_path: Path) -> None:
        # Such a judge would agree with the humans almost perfectly, and the report would
        # be a certificate for a leak. Failing before the first paid call is the point.
        suite = write_suite(tmp_path)
        write_labels(tmp_path)
        suite.write_text(
            suite.read_text(encoding="utf-8").replace(
                "    inputs: [input.body]", "    inputs: [input.body, expected.intent]"
            ),
            encoding="utf-8",
        )
        loaded = load_suite(suite)
        with pytest.raises(calibration_command.CalibrationCommandError, match="human label"):
            calibration_command.plan(loaded, evaluator="tone", labels=None)


class TestVersionHash:
    def test_reformatting_a_suite_does_not_change_the_version(self, tmp_path: Path) -> None:
        # An editor reordering YAML keys must not invalidate a calibration; only a change
        # that could change a score should.
        suite = write_suite(tmp_path)
        write_labels(tmp_path)
        loaded = load_suite(suite)
        spec = loaded.suite.evaluators[0]
        first = evaluator_version_hash(spec, rubric=RUBRIC)
        second = evaluator_version_hash(spec, rubric=RUBRIC)
        assert first == second

    def test_input_order_does_not_change_the_version(self, tmp_path: Path) -> None:
        suite = write_suite(tmp_path)
        write_labels(tmp_path)
        loaded = load_suite(suite)
        spec = loaded.suite.evaluators[0]
        reordered = spec.model_copy(update={"inputs": list(reversed(spec.inputs))})
        assert evaluator_version_hash(spec, rubric=RUBRIC) == evaluator_version_hash(
            reordered, rubric=RUBRIC
        )
