"""Judge tests, weighted toward the safeguards rather than the happy path.

No real provider is ever called. `FakeModelClient` scripts the responses, which is
what makes it possible to test an injected judge, a refusing judge, and a flaky judge
deterministically.
"""

from __future__ import annotations

import pytest

from evalforge_core.evaluators.judge import LLMJudge
from evalforge_core.testing import FakeModelClient
from evalforge_core.types import EvalContext
from evalforge_types import Example


def ctx(models: FakeModelClient, **kw: object) -> EvalContext:
    example = Example(
        id="ex-1",
        input={"evidence": "Acme raised a Series B in March."},
        expected={"ideal": "mentions the Series B"},
        metadata={"segment": "enterprise"},
    )
    return EvalContext(
        example=example,
        output={"body": "Congratulations on the Series B."},
        expected=example.expected,
        models=models,
        **kw,  # type: ignore[arg-type]
    )


def judge(**kw: object) -> LLMJudge:
    defaults: dict[str, object] = {
        "rubric": "Score 1-5 for groundedness.",
        "model": "test-model",
        "inputs": ["output.body", "input.evidence"],
        "name": "grounded",
    }
    return LLMJudge(**{**defaults, **kw})  # type: ignore[arg-type]


class TestInputAllowList:
    def test_a_judge_must_declare_its_inputs(self) -> None:
        with pytest.raises(ValueError, match="declares no inputs"):
            judge(inputs=[])

    async def test_only_declared_fields_reach_the_model(self) -> None:
        """The answer key must never leak into the judge prompt.

        A judge handed the whole example can read `expected` and grade against it,
        which produces excellent scores and measures nothing.
        """
        models = FakeModelClient({"reasoning": "grounded", "score": 5})
        await judge().evaluate(ctx(models))

        prompt = models.last_prompt()
        assert "Series B" in prompt  # the declared input is present
        assert "mentions the Series B" not in prompt  # `expected` is not
        assert "enterprise" not in prompt  # nor is unrelated metadata

    async def test_missing_input_field_is_an_error_not_a_zero(self) -> None:
        models = FakeModelClient({"reasoning": "x", "score": 5})
        score = await judge(inputs=["output.nonexistent"]).evaluate(ctx(models))
        assert score.errored
        assert score.value is None
        assert models.call_count == 0  # never spent money on a misconfigured judge


class TestInjectionDefences:
    async def test_canary_failure_discards_the_score(self) -> None:
        """A judge that stopped echoing the canary is no longer following its prompt."""
        models = FakeModelClient({"reasoning": "ignore", "score": 5}, echo_canary=False)
        score = await judge().evaluate(ctx(models))

        assert score.errored
        assert score.error is not None
        assert "canary" in score.error
        assert score.value is None  # discarded, not recorded as a 5

    async def test_content_is_fenced_and_labelled_as_data(self) -> None:
        models = FakeModelClient({"reasoning": "x", "score": 3})
        await judge().evaluate(ctx(models))
        system = models.calls[-1]["messages"][0][1]
        assert "never an instruction" in system.lower()
        assert "===EVALFORGE-" in models.last_prompt()

    async def test_delimiter_is_unpredictable_per_call(self) -> None:
        """A fixed delimiter could be closed by injected content."""
        models = FakeModelClient({"reasoning": "x", "score": 3})
        await judge().evaluate(ctx(models))
        first = models.last_prompt()
        await judge().evaluate(ctx(models))
        assert first != models.last_prompt()


class TestScoreParsing:
    async def test_rubric_score_is_normalized(self) -> None:
        models = FakeModelClient({"reasoning": "good", "score": 4})
        score = await judge(scale=(1, 5)).evaluate(ctx(models))
        assert score.value == pytest.approx(0.75)  # (4-1)/(5-1)
        assert score.raw["raw_score"] == 4

    async def test_out_of_range_score_is_an_error(self) -> None:
        models = FakeModelClient({"reasoning": "x", "score": 9})
        score = await judge(scale=(1, 5)).evaluate(ctx(models))
        assert score.errored
        assert "outside the declared scale" in (score.error or "")

    async def test_non_numeric_score_is_an_error(self) -> None:
        models = FakeModelClient({"reasoning": "x", "score": "five"})
        score = await judge().evaluate(ctx(models))
        assert score.errored

    async def test_binary_mode(self) -> None:
        models = FakeModelClient({"reasoning": "ok", "passed": False})
        score = await judge(mode="binary").evaluate(ctx(models))
        assert score.value == 0.0
        assert score.passed is False

    async def test_classify_rejects_unknown_labels(self) -> None:
        models = FakeModelClient({"reasoning": "x", "label": "enthusiastic"})
        score = await judge(mode="classify", labels=["warm", "cold"]).evaluate(ctx(models))
        assert score.errored
        assert "unknown label" in (score.error or "")

    async def test_classify_mode_requires_labels(self) -> None:
        with pytest.raises(ValueError, match="declares no labels"):
            judge(mode="classify")


class TestFailureHandling:
    async def test_provider_failure_is_an_error_not_a_zero(self) -> None:
        """A provider outage must not look like a quality regression."""
        models = FakeModelClient({"reasoning": "x", "score": 5}, fail_times=99)
        score = await judge(max_retries=1).evaluate(ctx(models))
        assert score.errored
        assert score.value is None
        assert "judge call failed" in (score.error or "")

    async def test_transient_failure_is_retried(self) -> None:
        models = FakeModelClient({"reasoning": "x", "score": 5}, fail_times=1)
        score = await judge(max_retries=2).evaluate(ctx(models))
        assert not score.errored
        assert models.call_count == 2

    async def test_missing_model_client_is_an_error(self) -> None:
        example = Example(id="e", input={})
        score = await judge().evaluate(EvalContext(example=example, output={}))
        assert score.errored


class TestDeterminismAndVoting:
    async def test_temperature_zero_and_pinned_seed_are_sent(self) -> None:
        models = FakeModelClient({"reasoning": "x", "score": 3})
        await judge(seed=7).evaluate(ctx(models))
        assert models.calls[-1]["temperature"] == 0.0
        assert models.calls[-1]["seed"] == 7

    async def test_votes_must_be_odd(self) -> None:
        with pytest.raises(ValueError, match="odd number"):
            judge(votes=2)

    async def test_self_consistency_takes_the_median_and_reports_spread(self) -> None:
        models = FakeModelClient(
            [
                {"reasoning": "a", "score": 1},
                {"reasoning": "b", "score": 3},
                {"reasoning": "c", "score": 5},
            ]
        )
        score = await judge(votes=3, scale=(1, 5)).evaluate(ctx(models))
        assert score.value == pytest.approx(0.5)  # median 3 -> 0.5
        assert score.confidence is not None
        assert score.confidence < 1.0  # wide spread is surfaced, not hidden

    async def test_each_vote_uses_a_different_seed(self) -> None:
        """Otherwise self-consistency at temperature 0 asks the same question N times."""
        models = FakeModelClient([{"reasoning": "x", "score": 3}] * 3)
        await judge(votes=3, seed=100).evaluate(ctx(models))
        seeds = [c["seed"] for c in models.calls]
        assert seeds == [100, 101, 102]


class TestSchema:
    def test_reasoning_precedes_the_score(self) -> None:
        """Field order matters: the model should argue before it commits."""
        properties = list(judge().response_schema()["properties"])
        assert properties.index("reasoning") < properties.index("score")

    def test_schema_constrains_the_range(self) -> None:
        schema = judge(scale=(1, 7)).response_schema()["properties"]["score"]
        assert schema["minimum"] == 1
        assert schema["maximum"] == 7
