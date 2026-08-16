"""The loop, closed: instrumented app -> SDK trace -> policy -> gate -> exit code.

This is the first test where all three packages built so far run together. It is the
proof that the SDK's output is directly consumable by the trajectory engine — a
seam that would otherwise only be verified by assumption.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import proofstep
from proofstep.client import Client
from proofstep.config import Config
from proofstep_core import Dataset, EvalResult, evaluate
from proofstep_core.evaluators import RegexMatch
from proofstep_trajectory import evaluate_policy, load_policy_file
from proofstep_trajectory.evaluator import TrajectoryEvaluator
from proofstep_types import Example, GateRule, Metric, Verdict

POLICY_PATH = Path(__file__).resolve().parents[3] / "evals" / "policies" / "email-approval.yaml"

Agent = Callable[[str], str]


def metric_of(result: EvalResult, key: str) -> Metric:
    found = result.metric(key)
    assert found is not None, f"metric {key!r} was not produced"
    return found


@pytest.fixture
def app(client: Client) -> Client:
    proofstep._client = client
    return client


def build_agent(*, approve_first: bool) -> Agent:
    """A miniature outbound-email agent, instrumented exactly as a user would."""

    @proofstep.tool("research_prospect")
    def research(prospect_id: str) -> dict[str, str]:
        return {"company": "Acme", "signal": "Series B"}

    @proofstep.tool("generate_email")
    def generate(research: dict[str, str]) -> str:
        return f"Congratulations on the {research['signal']}."

    @proofstep.tool("validate_claims")
    def validate(body: str) -> bool:
        return True

    @proofstep.tool("request_approval")
    def request_approval(body: str) -> str:
        return "pending"

    @proofstep.tool("approval_received")
    def approval_received() -> str:
        return "approved"

    @proofstep.tool("gmail.send")
    def send(to: str, thread_id: str, body: str) -> str:
        return "sent"

    def run(prospect_id: str) -> str:
        facts = research(prospect_id)
        body = generate(facts)
        validate(body)
        request_approval(body)
        # The whole point: swapping these two lines is invisible in the output and
        # fatal in the trajectory.
        if approve_first:
            approval_received()
            send(to="ok@x.com", thread_id="t1", body=body)
        else:
            send(to="ok@x.com", thread_id="t1", body=body)
            approval_received()
        proofstep.set_state(approval_status="approved")
        proofstep.set_metadata(
            suppression_list=[], reply_intent="interested", email_confidence=0.95
        )
        return body

    return run


class TestSdkOutputFeedsThePolicyEngine:
    def test_a_compliant_agent_passes(self, app: Client) -> None:
        run = build_agent(approve_first=True)
        with proofstep.capture("outreach") as captured:
            run("p1")

        result = evaluate_policy(load_policy_file(POLICY_PATH), captured[0])
        assert result.passed, result.format()

    def test_send_before_approval_is_caught_from_a_real_sdk_trace(self, app: Client) -> None:
        run = build_agent(approve_first=False)
        with proofstep.capture("outreach") as captured:
            body = run("p1")

        result = evaluate_policy(load_policy_file(POLICY_PATH), captured[0])

        # The email itself is fine. Only the trajectory is wrong.
        assert body == "Congratulations on the Series B."
        assert not result.passed
        assert "no-send-before-approval" in [f.rule_id for f in result.blocking_failures]

    def test_the_failure_points_at_a_real_span_in_the_captured_trace(self, app: Client) -> None:
        run = build_agent(approve_first=False)
        with proofstep.capture("outreach") as captured:
            run("p1")

        trace = captured[0]
        failure = next(
            f
            for f in evaluate_policy(load_policy_file(POLICY_PATH), trace).failures
            if f.rule_id == "no-send-before-approval"
        )
        span = trace.find_span(failure.offending_span_id or "")
        assert span is not None, "the failure referenced a span that is not in the trace"
        assert span.tool_name == "gmail.send"

    def test_tool_decorator_captures_arguments_for_argument_conditions(self, app: Client) -> None:
        """`@tool` must record args, or half the policy kinds silently cannot fire."""
        run = build_agent(approve_first=True)
        with proofstep.capture("outreach") as captured:
            run("p1")

        send = next(s for s in captured[0].spans if s.tool_name == "gmail.send")
        assert send.tool_args is not None
        assert send.tool_args["to"] == "ok@x.com"
        assert send.tool_args["thread_id"] == "t1"


class TestFullLoopThroughTheEvaluationEngine:
    async def test_a_trajectory_violation_fails_the_gate_with_a_nonzero_exit(
        self, app: Client
    ) -> None:
        """dataset -> instrumented task -> policy evaluator -> gate -> exit 1."""
        run = build_agent(approve_first=False)

        async def task(example: Example) -> proofstep.Captured:
            with proofstep.capture("outreach") as captured:
                output = run(example.input["prospect_id"])
            return proofstep.Captured(output={"body": output}, trace=captured[0])

        dataset = Dataset([Example(id=f"p{i}", input={"prospect_id": f"p{i}"}) for i in range(3)])

        result = await evaluate(
            dataset=dataset,
            task=task,
            evaluators=[
                TrajectoryEvaluator(POLICY_PATH, name="approval_trajectory"),
                RegexMatch(field="output.body", deny=[r"\[Your Name\]"], name="no_placeholders"),
            ],
            gates=[
                GateRule(metric_key="approval_trajectory", minimum=1.0),
                GateRule(metric_key="no_placeholders", minimum=1.0),
            ],
        )

        assert result.gates.verdict is Verdict.FAIL
        assert result.exit_code == 1

        # The output-level check passes; only the trajectory gate blocks. That
        # asymmetry is the product thesis in one assertion.
        assert metric_of(result, "no_placeholders").value == 1.0
        assert metric_of(result, "approval_trajectory").value == 0.0

        blocking = result.gates.blocking_failures
        assert [f.metric_key for f in blocking] == ["approval_trajectory"]

    async def test_a_compliant_agent_passes_the_same_gates(self, app: Client) -> None:
        run = build_agent(approve_first=True)

        async def task(example: Example) -> proofstep.Captured:
            with proofstep.capture("outreach") as captured:
                output = run(example.input["prospect_id"])
            return proofstep.Captured(output={"body": output}, trace=captured[0])

        result = await evaluate(
            dataset=Dataset([Example(id="p1", input={"prospect_id": "p1"})]),
            task=task,
            evaluators=[TrajectoryEvaluator(POLICY_PATH, name="approval_trajectory")],
            gates=[GateRule(metric_key="approval_trajectory", minimum=1.0)],
        )

        assert result.gates.verdict is Verdict.PASS
        assert result.exit_code == 0


class TestLocalMode:
    def test_capture_works_with_no_api_key_and_no_server(self) -> None:
        """Local-first: the loop must run before anyone has an account."""
        proofstep.init(project="local", api_key=None, export=False)

        @proofstep.tool("do_thing")
        def do_thing(x: int) -> int:
            return x * 2

        with proofstep.capture("workflow") as captured:
            assert do_thing(21) == 42

        assert len(captured) == 1
        assert [s.tool_name for s in captured[0].spans] == ["do_thing"]


def test_config_defaults_are_conservative() -> None:
    """Safe defaults are a security control, not a preference."""
    config = Config()
    assert config.capture_mode.value == "redacted"
    assert config.sends is False  # no api key -> nothing leaves the process
