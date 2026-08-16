"""Every reference suite loads, and every injected regression is caught.

Lives in the repository-level `tests/` tree rather than under `packages/`, because it names the
example domains and `scripts/check-domain-leak.sh` refuses domain terms in platform code. That
check caught this file on its first run, which is the control working: a platform test that
enumerates Davis and AdaptQuiz suites is platform code that knows about them.

Two properties, and the second is the one that matters. A suite that merely *parses* proves
nothing; a suite whose gates cannot fire is decoration. So each entry below names the flag that
injects a real defect and the gate that must catch it — and the test fails if the run passes.

Run against the committed fixtures with the stub judge, so this needs no provider and no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITES = ROOT / "evals" / "suites"
STUB = "examples.stub_judge:make_client"

#: Every reference suite, and whether it needs a judge client.
REFERENCE_SUITES: tuple[str, ...] = (
    "davis-lead-ranking",
    "davis-research",
    "davis-email",
    "davis-reply-intent",
    "davis-agent-policy",
    "davis-meetings",
    "quiz-ingestion",
    "quiz-questions",
    "quiz-learning",
    "quiz-security",
    # The Phase 7 calibration reference, which is also a suite.
    "reply-tone",
    "reply-intent",
)

#: (flag, suite, the gate that must fire). Each row is a claim that a specific defect is caught by
#: a specific mechanism — which is what a reference suite is for.
REGRESSIONS: tuple[tuple[str, str, str], ...] = (
    # A ranking change that promotes disqualified leads. NDCG barely moves; the deterministic gate
    # is what catches it, which is the whole argument for protected metrics.
    ("DAVIS_BREAK_RANKING", "davis-lead-ranking", "no_top_disqualified"),
    ("DAVIS_BREAK_CITATIONS", "davis-research", "citation_present"),
    # Cited, resolvable, and wrong — the failure only a judge can see.
    ("DAVIS_BREAK_GROUNDING", "davis-research", "claim_groundedness"),
    ("DAVIS_BREAK_PLACEHOLDERS", "davis-email", "no_placeholders"),
    ("DAVIS_BREAK_CLAIMS", "davis-email", "approved_claim_compliance"),
    # The canonical protected-metric case: the sliced floor fires, not the aggregate.
    ("DAVIS_BREAK_UNSUBSCRIBE", "davis-reply-intent", "classes_recall"),
    ("DAVIS_BREAK_POLICY", "davis-agent-policy", "agent_policy_compliance"),
    ("DAVIS_BREAK_DATES", "davis-meetings", "date_extraction"),
    ("QUIZ_BREAK_OFFSETS", "quiz-ingestion", "citation_location"),
    ("QUIZ_BREAK_ANSWER_KEY", "quiz-questions", "single_correct_answer"),
    ("QUIZ_BREAK_DEDUP", "quiz-questions", "duplicate_rate"),
    ("QUIZ_BREAK_MASTERY", "quiz-learning", "mastery_auc"),
    # The trajectory case: caught in the trace, invisible in half the outputs.
    ("QUIZ_BREAK_ISOLATION", "quiz-security", "tenant_isolation"),
)


#: The installed console script, resolved from the running interpreter's environment so the test
#: exercises the same entry point a user does.
CLI = str(Path(sys.executable).parent / "proofstep")


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [CLI, *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        timeout=300,
        check=False,
    )


class TestSuitesLoad:
    @pytest.mark.parametrize("suite", REFERENCE_SUITES)
    def test_it_validates(self, suite: str) -> None:
        result = run_cli("validate", str(SUITES / f"{suite}.yaml"))
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("suite", REFERENCE_SUITES)
    def test_it_plans_without_calling_a_model(self, suite: str) -> None:
        # `--dry-run` reports the judge-call count without making one. A suite that cannot be
        # planned for free cannot be reviewed before it costs money.
        result = run_cli("eval", str(SUITES / f"{suite}.yaml"), "--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "no model calls were made" in result.stdout


@pytest.mark.slow
class TestRegressionsAreCaught:
    @pytest.mark.parametrize(
        ("flag", "suite", "gate"), REGRESSIONS, ids=[r[0] for r in REGRESSIONS]
    )
    def test_the_gate_fires(self, flag: str, suite: str, gate: str, tmp_path: Path) -> None:
        """The load-bearing test in this file.

        A suite whose gates cannot fire gives false assurance, which is worse than no suite. `!`
        semantics: the run is *expected* to fail, and a zero exit code here means the defect
        shipped.
        """
        result = run_cli(
            "eval",
            str(SUITES / f"{suite}.yaml"),
            "-o",
            str(tmp_path / "report.json"),
            "--model-client",
            STUB,
            env={flag: "1"},
        )
        assert result.returncode == 1, (
            f"{flag} did not fail {suite} (exit {result.returncode}). A gate that cannot fire is "
            f"a gate that gives false assurance.\n{result.stdout[-2000:]}"
        )
        assert gate in result.stdout, (
            f"{flag} failed {suite}, but not through {gate!r} — so the suite is catching this "
            f"defect by accident rather than by design.\n{result.stdout[-2000:]}"
        )

    @pytest.mark.parametrize("suite", REFERENCE_SUITES)
    def test_a_clean_run_passes(self, suite: str, tmp_path: Path) -> None:
        # The complement. Gates that fire on a healthy run get bypassed, and then they protect
        # nothing at all.
        result = run_cli(
            "eval",
            str(SUITES / f"{suite}.yaml"),
            "-o",
            str(tmp_path / "report.json"),
            "--model-client",
            STUB,
        )
        assert result.returncode == 0, result.stdout[-3000:]
