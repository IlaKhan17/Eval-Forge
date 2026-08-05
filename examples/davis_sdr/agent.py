"""The outbound agent, instrumented — the trajectory suite's subject.

Every property this suite checks is invisible to output evaluation. The email is byte-identical
whether it was approved or not, whether the recipient was suppressed, whether it was the second
copy of a message already sent. The difference is only in what the agent *did*.

## How the fixture and the agent relate

The fixture supplies **adversarial situations** — a suppressed recipient, an unsubscribed
prospect, a low-confidence draft, a calendar conflict — and a correct agent handles all of them
compliantly. So the suite's expected state is `compliance = 1.0`, and the gate is an absolute
floor at exactly that.

`DAVIS_BREAK_POLICY=1` makes the agent take the shortcut in each situation: send without
waiting for approval, ignore the suppression check, send to someone who unsubscribed. That is
the regression the policy exists to catch, and it is what the CI check demonstrates.

Getting this the other way round — a fixture of scenarios the agent deliberately fails — would
make the suite measure "does the policy still contain 13 rules" rather than "is the agent
behaving", and a compliance gate at 1.0 over deliberately-failing fixtures could never pass.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import evalforge

#: A run's send allowance. The `limit` rule gates on this.
DAILY_SEND_LIMIT = 3
#: Below this a draft must go to a human, per the `conditional` rule.
REVIEW_THRESHOLD = 0.7
#: A run's search budget, gated as a warning rather than a block.
SEARCH_BUDGET = 8


async def _tool(name: str, *, args: dict[str, Any] | None = None, output: Any = None) -> None:
    with evalforge.start_span(name, span_type="tool", tool_name=name) as span:
        await asyncio.sleep(0)
        if args:
            span.set_args(args)
        if output is not None:
            span.set_output(output)


async def _guardrail(name: str, *, output: Any = None) -> None:
    with evalforge.start_span(name, span_type="guardrail", tool_name=name) as span:
        await asyncio.sleep(0)
        if output is not None:
            span.set_output(output)


async def run_scenario(example: Any) -> evalforge.Captured:
    """Handle one adversarial situation, traced.

    Reads as the agent's actual control flow rather than as a switch over scenarios, because
    that is what it is: each hazard is a condition the agent checks, and `broken` is what makes
    it skip the check.
    """
    scenario = str(example.input.get("scenario", "clean"))
    recipient = str(example.input.get("to", "buyer@example.com"))
    thread = str(example.input.get("thread_id", "t-1"))
    confidence = float(example.input.get("confidence", 0.95))
    unsubscribed = bool(example.input.get("unsubscribed"))
    suppressed = bool(example.input.get("suppressed"))
    conflicts = bool(example.input.get("calendar_conflict"))
    attendee = str(example.input.get("attendee") or recipient)
    broken = os.environ.get("DAVIS_BREAK_POLICY") == "1"

    sent = False
    with evalforge.capture("davis.outbound") as captured:
        evalforge.set_metadata(scenario=scenario)
        # State the `conditional` rule reads. A rule cannot check what the trace does not carry.
        evalforge.set_state(unsubscribed=unsubscribed)

        # Research, bounded. A correct agent stops at the budget; a broken one keeps going,
        # which the warn-severity limit rule reports without blocking the merge.
        # Two, not three: three identical consecutive searches trips the `no_loop` rule at
        # min_repeats=3, and it is right to — that is what a stuck agent looks like.
        for _ in range(SEARCH_BUDGET + 4 if broken else 2):
            await _tool("web_search", output={"hits": 3})
        await _tool("research_prospect", output={"company": "Acme"})
        await _tool("generate_email", output={"subject": "Hi", "confidence": confidence})

        # Required controls. Both are `required_action` rules, because a guardrail that is
        # merely *usually* run is not a control.
        if not broken:
            await _guardrail("guardrail.injection_scan", output={"clean": True})
            await _guardrail("validate_claims", output={"unsupported": 0})

        # A low-confidence draft goes to a human. Skipping it is the failure the conditional
        # rule catches, and it is invisible in the email.
        if confidence < REVIEW_THRESHOLD and not broken:
            await _tool("human_review", output={"approved": True})

        # `stop` rather than an early return: the trace snapshot is taken when this block exits,
        # so returning from inside it would hand the evaluator an empty trace.
        stop = False
        if unsubscribed:
            # The compliant response to an unsubscribe is to suppress and stop. A broken agent
            # falls through to the send, which the conditional rule catches.
            await _tool("suppression.add", args={"to": recipient})
            stop = not broken

        if not stop:
            # Suppression is checked before sending, and the outcome is recorded as an argument
            # so the `argument_condition` rule can read it. A check whose result never reaches
            # the trace is unauditable.
            blocked = suppressed and not broken
            await _tool(
                "check_suppression",
                args={"suppressed": suppressed and not broken},
                output={"blocked": blocked},
            )
            stop = blocked

        if not stop:
            if not broken:
                await _tool("request_approval")
                await _tool("approval_received", output={"approver": "dana"})

            for index in range(DAILY_SEND_LIMIT + 2 if broken else 1):
                await _tool(
                    "gmail.send",
                    args={
                        "to": recipient,
                        "thread_id": thread,
                        "suppressed": False,
                        "copy": index,
                    },
                )
            sent = True

            if example.input.get("book_meeting"):
                # A conflicting slot or a malformed attendee is refused. Booking anyway is what
                # the two `argument_condition` rules catch.
                await _tool(
                    "book_meeting",
                    args={
                        "attendees": ["not-an-email" if broken else attendee],
                        "conflicts": conflicts and broken,
                    },
                )

    return _finish(captured, scenario, sent=sent)


def _finish(captured: list[Any], scenario: str, *, sent: bool) -> evalforge.Captured:
    """Package the trace for the runner.

    `Captured`, not a bare dict: the runner reads `.trace` off the return value, which is what
    lets a trajectory evaluator see the trace with no adapter between them.
    """
    trace = captured[0] if captured else None
    spans = trace.spans if trace else []
    return evalforge.Captured(
        output={
            "scenario": scenario,
            "sent": sent and any(span.tool_name == "gmail.send" for span in spans),
            "span_count": len(spans),
        },
        trace=trace,
    )
