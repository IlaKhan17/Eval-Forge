"""A deterministic stand-in for a judge model, so the reference suites run with no provider.

**This is a stub, and it must never be mistaken for a judge.** It does not read the rubric and
it has no opinion. What it does is exercise the whole judge *machinery* end to end — structured
output, the canary check, the input allow-list, cost and latency accounting, self-consistency
voting, and the calibration path — against fixtures, for free, on a fork pull request where
there are no secrets by design.

It answers by looking for the property the fixture encodes, with deliberate imperfection: a
small, fixed share of examples get the wrong answer. A stub that were always right would make
every calibration report show κ = 1.0, which is both useless as a demonstration and actively
misleading about what calibration looks like.

Swap it for a real client with one flag:

    proofstep eval suite.yaml --model-client myproject.models:make_client

The interface is `proofstep_core.types.ModelClient`, which is four keyword arguments and a
`ModelResponse`. Nothing here depends on Proofstep internals beyond that.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from proofstep_core.types import Message, ModelResponse

#: Share of answers the stub deliberately gets wrong, so calibration reports look like real ones
#: rather than reporting a perfect kappa. Deterministic in the content hash rather than random, so
#: a re-run produces the same report — an unreproducible calibration in a reproducibility tool
#: would be absurd.
#:
#: 1%, chosen so the reference suites' *principled* thresholds pass with headroom rather than the
#: thresholds being loosened to fit the stub. That is the right direction: a false-pass limit of
#: 0.04 on outbound claims about real companies is a considered number, and tuning it upward to
#: accommodate a fixture would be exactly the anti-pattern this project argues against.
#:
#: The rate is a knob for the demonstration and says nothing about real judges. The point of
#: shipping calibration is that you measure yours rather than assume a number.
ERROR_RATE = 0.01

#: Priced like a small model, so cost gates and the calibration report have real numbers to
#: show rather than zeroes.
COST_PER_CALL = Decimal("0.00021")
LATENCY_MS = 640


class StubModelClient:
    """A `ModelClient` that answers from the content rather than from a model."""

    def __init__(self, *, error_rate: float = ERROR_RATE) -> None:
        self.error_rate = error_rate
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[Message],
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,  # noqa: ARG002 — part of the protocol
        seed: int | None = None,
        max_tokens: int | None = None,  # noqa: ARG002 — part of the protocol
        timeout: float = 60.0,  # noqa: ARG002, ASYNC109 — the ModelClient protocol's shape
    ) -> ModelResponse:
        self.calls += 1
        content = "\n".join(message.content for message in messages)
        canary = _canary(content)

        properties = (response_schema or {}).get("properties", {})
        verdict = self._decide(content, properties, seed=seed)

        payload = {"reasoning": _reasoning(verdict), **verdict, "canary": canary}
        return ModelResponse(
            content=json.dumps(payload),
            model=model,
            prompt_tokens=len(content) // 4,
            completion_tokens=40,
            cost=COST_PER_CALL,
            latency_ms=LATENCY_MS,
            parsed=payload,
        )

    # ------------------------------------------------------------------ deciding

    def _decide(
        self, content: str, properties: dict[str, Any], *, seed: int | None
    ) -> dict[str, Any]:
        """Answer in whichever shape the schema asks for.

        The schema is the contract, not a hint: a judge in `classify` mode gets a `label`, one in
        `binary` mode a `passed`, one in `rubric` mode a `score`. Answering in the wrong shape
        would be rejected by the judge's own parser, which is itself worth exercising.
        """
        good = self._looks_good(content)
        if self._should_err(content, seed):
            good = not good

        if "label" in properties:
            labels = properties["label"].get("enum") or ["acceptable", "unacceptable"]
            # The convention across the reference rubrics: the first label is the positive one.
            return {"label": labels[0] if good else labels[-1]}
        if "passed" in properties:
            return {"passed": good}

        low = int(properties.get("score", {}).get("minimum", 1))
        high = int(properties.get("score", {}).get("maximum", 5))
        # Not the extremes. A real judge clusters in the upper middle, and a stub that answered
        # 5 or 1 would make the scale-compression statistic meaningless.
        return {"score": high - 1 if good else low + 1}

    def _looks_good(self, content: str) -> bool:
        """Read the property the fixture encodes, from the content block only.

        Crude by design. These markers are what the reference fixtures actually put in front of
        a judge — a claim beside its source, a body beside its evidence — and matching on them
        keeps the stub's answers *correlated with the truth*, which is what makes the resulting
        calibration report resemble a real one.
        """
        # Only the section after the judge's "# Content to evaluate" heading. Reading the whole
        # prompt looks equivalent and is not: a well-written rubric quotes its boundary cases, so
        # the rubric text contains the very markers this function looks for. The first version did
        # read everything, and every claim came back unsupported because the groundedness rubric
        # mentions the Lisbon example.
        marker = "# Content to evaluate"
        body = content.split(marker, 1)[1] if marker in content else content
        lowered = body.lower()
        for marker in (
            "opening an office in lisbon",  # the unsupported claim in the research fixture
            "guarantees_3x_pipeline",  # the unapproved claim in the email fixture
            "[first name]",  # a leaked placeholder
            "unassigned",  # a meeting action item with no owner
            "are always constant",  # a wrong distractor keyed correct
            "apply only to integers",
        ):
            if marker in lowered:
                return False
        return True

    def _should_err(self, content: str, seed: int | None) -> bool:
        """Deterministically wrong on a fixed share of inputs.

        Hashed rather than random so the same content always gets the same answer, and salted
        with the seed so self-consistency voting sees genuine variation across votes instead of
        the same answer N times.
        """
        digest = hashlib.sha256(f"{seed}\x00{content}".encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") / 2**32
        return bucket < self.error_rate


def make_client() -> StubModelClient:
    """Factory for `--model-client examples.stub_judge:make_client`."""
    return StubModelClient()


def make_perfect_client() -> StubModelClient:
    """A stub that never errs.

    For the one place it is the right answer: proving a *deterministic* gate is unaffected by
    judge noise. Never use it for a calibration demonstration — it would report κ = 1.0 and
    teach the opposite of what calibration is for.
    """
    return StubModelClient(error_rate=0.0)


def _canary(content: str) -> str:
    """Echo the canary the judge planted in its system prompt.

    The judge discards any answer whose canary does not match, which is how it detects a model
    that started following the evaluated content instead of the rubric. A stub that ignored it
    would fail every call — so echoing it correctly is what proves the check is wired up.
    """
    for line in content.splitlines():
        marker = 'Set "canary" to exactly "'
        if marker in line:
            return line.split(marker, 1)[1].split('"', 1)[0]
    return ""


def _reasoning(verdict: dict[str, Any]) -> str:
    return (
        "Stub judge: answered from fixture markers, not from the rubric. "
        f"Verdict: {json.dumps(verdict, sort_keys=True)}."
    )
