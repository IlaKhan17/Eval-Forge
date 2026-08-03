"""Suite loading and validation.

Every check here fires before a single model call. A suite of 500 examples across
six judges costs real money, so a misconfiguration must be caught in milliseconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evalforge_cli.suite.loader import SuiteError, load_suite

MINIMAL = """
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: minimal
dataset:
  path: data.jsonl
task:
  entrypoint: mymod:run
evaluators:
  - name: acc
    type: exact_match
    field: output.answer
"""


def write(tmp_path: Path, body: str, *, name: str = "suite.yaml", data: bool = True) -> Path:
    if data:
        (tmp_path / "data.jsonl").write_text('{"id": "a", "input": {}}\n', encoding="utf-8")
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestBasics:
    def test_a_minimal_suite_loads(self, tmp_path: Path) -> None:
        loaded = load_suite(write(tmp_path, MINIMAL))
        assert loaded.suite.name == "minimal"
        assert len(loaded.suite.evaluators) == 1

    def test_a_missing_file_is_reported_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(SuiteError, match="not found"):
            load_suite(tmp_path / "nope.yaml")

    def test_invalid_yaml_reports_a_position(self, tmp_path: Path) -> None:
        with pytest.raises(SuiteError, match=r":\d+:\d+: invalid YAML"):
            load_suite(write(tmp_path, "name: x\nevaluators:\n  - [unclosed\n"))

    def test_an_unsupported_api_version_is_refused(self, tmp_path: Path) -> None:
        """Suite files live in users' repos, so the format is versioned from day one."""
        body = MINIMAL.replace("evalforge.dev/v1", "evalforge.dev/v99")
        with pytest.raises(SuiteError, match="unsupported apiVersion"):
            load_suite(write(tmp_path, body))

    def test_an_unknown_field_is_rejected(self, tmp_path: Path) -> None:
        """A typo'd key silently ignored produces a suite that looks configured."""
        body = MINIMAL + "\nconcurrancy: 4\n"
        with pytest.raises(SuiteError, match="concurrancy"):
            load_suite(write(tmp_path, body))

    def test_paths_resolve_relative_to_the_suite_not_the_cwd(self, tmp_path: Path) -> None:
        """A suite must behave the same from the repo root or from its own folder."""
        nested = tmp_path / "suites"
        nested.mkdir()
        (tmp_path / "data.jsonl").write_text('{"id":"a","input":{}}\n', encoding="utf-8")
        body = MINIMAL.replace("path: data.jsonl", "path: ../data.jsonl")
        loaded = load_suite(write(nested, body, data=False))
        assert loaded.resolve_path("../data.jsonl").exists()


class TestDatasetReference:
    def test_exactly_one_source_is_required(self, tmp_path: Path) -> None:
        body = MINIMAL.replace("  path: data.jsonl", "  name: remote\n  path: data.jsonl")
        with pytest.raises(SuiteError, match="exactly one"):
            load_suite(write(tmp_path, body))

    def test_neither_source_is_rejected(self, tmp_path: Path) -> None:
        body = MINIMAL.replace("dataset:\n  path: data.jsonl", "dataset:\n  split: test")
        with pytest.raises(SuiteError, match="exactly one"):
            load_suite(write(tmp_path, body))

    def test_a_missing_local_dataset_is_caught_before_the_run(self, tmp_path: Path) -> None:
        with pytest.raises(SuiteError, match="does not exist"):
            load_suite(write(tmp_path, MINIMAL, data=False))


class TestJudgeRequirements:
    JUDGE = """
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: judged
dataset:
  path: data.jsonl
task:
  entrypoint: m:r
evaluators:
  - name: grounded
    type: llm_judge
    rubric: be strict
    model: pinned-model-v1
    inputs: [output.body]
"""

    def test_a_valid_judge_loads(self, tmp_path: Path) -> None:
        assert load_suite(write(tmp_path, self.JUDGE)).suite.evaluators[0].type == "llm_judge"

    def test_a_judge_without_inputs_is_refused(self, tmp_path: Path) -> None:
        """Without an allow-list the judge can read `expected` and grade itself."""
        body = self.JUDGE.replace("    inputs: [output.body]\n", "")
        with pytest.raises(SuiteError, match="grade against the answer key"):
            load_suite(write(tmp_path, body))

    def test_a_judge_without_a_pinned_model_is_refused(self, tmp_path: Path) -> None:
        """An unpinned judge silently invalidates every historical comparison."""
        body = self.JUDGE.replace("    model: pinned-model-v1\n", "")
        with pytest.raises(SuiteError, match="must pin a `model`"):
            load_suite(write(tmp_path, body))

    def test_a_judge_without_a_rubric_is_refused(self, tmp_path: Path) -> None:
        body = self.JUDGE.replace("    rubric: be strict\n", "")
        with pytest.raises(SuiteError, match="needs `rubric`"):
            load_suite(write(tmp_path, body))

    def test_a_missing_rubric_file_is_caught(self, tmp_path: Path) -> None:
        body = self.JUDGE.replace("    rubric: be strict", "    rubric_path: rubrics/absent.md")
        with pytest.raises(SuiteError, match="does not exist"):
            load_suite(write(tmp_path, body))


class TestGateValidation:
    def test_a_gate_naming_an_unknown_metric_is_an_error(self, tmp_path: Path) -> None:
        """The most dangerous configuration bug: green CI measuring nothing."""
        body = MINIMAL + "\ngates:\n  acccuracy:\n    minimum: 0.9\n"
        with pytest.raises(SuiteError, match="matches no evaluator"):
            load_suite(write(tmp_path, body))

    def test_the_error_lists_what_is_available(self, tmp_path: Path) -> None:
        body = MINIMAL + "\ngates:\n  typo:\n    minimum: 0.9\n"
        with pytest.raises(SuiteError, match="Declared evaluators: acc"):
            load_suite(write(tmp_path, body))

    def test_a_gate_with_no_condition_is_refused(self, tmp_path: Path) -> None:
        body = MINIMAL + "\ngates:\n  acc:\n    blocking: true\n"
        with pytest.raises(SuiteError, match="declares no condition"):
            load_suite(write(tmp_path, body))

    def test_a_regression_gate_without_a_baseline_is_refused(self, tmp_path: Path) -> None:
        """max_regression with nothing to regress against cannot ever fire."""
        body = MINIMAL + "\nbaseline:\n  strategy: none\ngates:\n  acc:\n    max_regression: 0.02\n"
        with pytest.raises(SuiteError, match="nothing to regress against"):
            load_suite(write(tmp_path, body))

    def test_a_valid_gate_loads(self, tmp_path: Path) -> None:
        body = MINIMAL + "\ngates:\n  acc:\n    minimum: 0.9\n"
        assert load_suite(write(tmp_path, body)).suite.gates["acc"].minimum == 0.9


class TestMetricCollisions:
    def test_two_evaluators_producing_one_metric_are_refused(self, tmp_path: Path) -> None:
        """A classification evaluator named `intent` emits `intent_accuracy`.

        If another evaluator is called `intent_accuracy`, both write one key and the
        reported value depends on evaluation order.
        """
        body = """
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: collide
dataset:
  path: data.jsonl
task:
  entrypoint: m:r
evaluators:
  - name: intent_accuracy
    type: exact_match
    field: output.intent
  - name: intent
    type: classification
"""
        with pytest.raises(SuiteError, match="both produce the metric 'intent_accuracy'"):
            load_suite(write(tmp_path, body))

    def test_distinct_names_are_fine(self, tmp_path: Path) -> None:
        body = """
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: fine
dataset:
  path: data.jsonl
task:
  entrypoint: m:r
evaluators:
  - name: exact_intent
    type: exact_match
    field: output.intent
  - name: classes
    type: classification
"""
        assert len(load_suite(write(tmp_path, body)).suite.evaluators) == 2

    def test_duplicate_evaluator_names_are_refused(self, tmp_path: Path) -> None:
        body = MINIMAL + "  - name: acc\n    type: regex\n    deny: ['x']\n"
        with pytest.raises(SuiteError, match="duplicate evaluator name"):
            load_suite(write(tmp_path, body))


class TestInterpolation:
    def test_a_variable_is_substituted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_MODEL", "model-x")
        body = MINIMAL.replace("    field: output.answer", "    field: ${MY_MODEL}")
        assert load_suite(write(tmp_path, body)).suite.evaluators[0].field == "model-x"

    def test_a_default_is_used_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ABSENT_VAR", raising=False)
        body = MINIMAL.replace("    field: output.answer", "    field: ${ABSENT_VAR:-fallback}")
        assert load_suite(write(tmp_path, body)).suite.evaluators[0].field == "fallback"

    def test_a_missing_variable_with_no_default_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Substituting an empty string would produce a suite that measures the
        wrong thing while appearing to work."""
        monkeypatch.delenv("REQUIRED_VAR", raising=False)
        body = MINIMAL.replace("    field: output.answer", "    field: ${REQUIRED_VAR}")
        with pytest.raises(SuiteError, match="is not set and has no default"):
            load_suite(write(tmp_path, body))

    def test_secret_looking_variables_are_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tracked so they can be excluded from anything persisted server-side."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-not-real")
        body = MINIMAL.replace("    field: output.answer", "    field: ${OPENAI_API_KEY}")
        assert "OPENAI_API_KEY" in load_suite(write(tmp_path, body)).resolved_secrets


class TestComposition:
    def test_a_child_inherits_and_overrides(self, tmp_path: Path) -> None:
        write(tmp_path, MINIMAL, name="base.yaml")
        child = """
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: child
extends: base.yaml
execution:
  concurrency: 32
"""
        loaded = load_suite(write(tmp_path, child, name="child.yaml", data=False))
        assert loaded.suite.name == "child"
        assert loaded.suite.execution.concurrency == 32
        assert len(loaded.suite.evaluators) == 1  # inherited

    def test_lists_replace_rather_than_concatenate(self, tmp_path: Path) -> None:
        """Concatenating would make it impossible to remove an inherited evaluator."""
        write(tmp_path, MINIMAL, name="base.yaml")
        child = """
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: child
extends: base.yaml
evaluators:
  - name: only_mine
    type: regex
    deny: ["x"]
"""
        loaded = load_suite(write(tmp_path, child, name="child.yaml", data=False))
        assert [e.name for e in loaded.suite.evaluators] == ["only_mine"]

    def test_inheritance_is_one_level_only(self, tmp_path: Path) -> None:
        """Deep config hierarchies make the effective configuration unknowable."""
        write(tmp_path, MINIMAL, name="grand.yaml")
        write(
            tmp_path,
            "apiVersion: evalforge.dev/v1\nkind: EvalSuite\nname: mid\nextends: grand.yaml\n",
            name="mid.yaml",
            data=False,
        )
        child = "apiVersion: evalforge.dev/v1\nkind: EvalSuite\nname: kid\nextends: mid.yaml\n"
        with pytest.raises(SuiteError, match="one level deep"):
            load_suite(write(tmp_path, child, name="kid.yaml", data=False))


class TestOverrides:
    def test_a_scalar_is_overridden_with_its_type_preserved(self, tmp_path: Path) -> None:
        loaded = load_suite(
            write(tmp_path, MINIMAL + "\nexecution:\n  concurrency: 4\n"),
            overrides={"execution.concurrency": "16"},
        )
        assert loaded.suite.execution.concurrency == 16

    def test_a_boolean_is_coerced(self, tmp_path: Path) -> None:
        body = MINIMAL + "\ngates:\n  acc:\n    minimum: 0.9\n    blocking: true\n"
        loaded = load_suite(write(tmp_path, body), overrides={"gates.acc.blocking": "false"})
        assert loaded.suite.gates["acc"].blocking is False

    def test_an_override_of_an_unknown_field_is_an_error(self, tmp_path: Path) -> None:
        """A silently ignored --set is worse than a refused one."""
        with pytest.raises(SuiteError, match="does not match any field"):
            load_suite(write(tmp_path, MINIMAL), overrides={"nope.nothing": "1"})


class TestHints:
    def test_a_judge_heavy_suite_is_flagged(self, tmp_path: Path) -> None:
        """Advice, not an error: most things should not be judged by a model."""
        body = """
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: judgey
dataset:
  path: data.jsonl
task:
  entrypoint: m:r
evaluators:
  - name: a
    type: llm_judge
    rubric: r
    model: m
    inputs: [output.x]
  - name: b
    type: llm_judge
    rubric: r
    model: m
    inputs: [output.x]
  - name: c
    type: exact_match
"""
        hints = load_suite(write(tmp_path, body)).hints
        assert any("judge-heavy suite" in h for h in hints)

    def test_gating_on_an_uncalibrated_judge_is_flagged(self, tmp_path: Path) -> None:
        body = """
apiVersion: evalforge.dev/v1
kind: EvalSuite
name: uncal
dataset:
  path: data.jsonl
task:
  entrypoint: m:r
evaluators:
  - name: grounded
    type: llm_judge
    rubric: r
    model: m
    inputs: [output.x]
gates:
  grounded:
    minimum: 0.9
"""
        hints = load_suite(write(tmp_path, body)).hints
        assert any("uncalibrated judge" in h for h in hints)

    def test_a_suite_with_no_blocking_gate_is_flagged(self, tmp_path: Path) -> None:
        body = MINIMAL + "\ngates:\n  acc:\n    minimum: 0.5\n    blocking: false\n"
        hints = load_suite(write(tmp_path, body)).hints
        assert any("never fail CI" in h for h in hints)
