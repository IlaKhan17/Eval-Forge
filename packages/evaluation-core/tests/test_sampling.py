"""Online-evaluation sampling.

The rate-accuracy tests use a binomial tolerance rather than an arbitrary epsilon: a
sample of 10 000 traces at 1 % has a standard deviation of about 10, so a three-standard-
deviation band is the honest assertion. A tighter one would be flaky, and a looser one
would not catch a real bias in the hash.
"""

from __future__ import annotations

import math

import pytest

from proofstep_core.sampling import (
    EscalationBudget,
    SamplingRule,
    bucket_of,
    decide,
    expected_paid_calls,
    is_sampled,
)


def trace_ids(n: int, prefix: str = "t") -> list[str]:
    # Hex ids, as the SDK emits. Sequential rather than random so the test is
    # deterministic, and adjacent-input sensitivity is exactly what a hash must have.
    return [f"{prefix}{i:032x}" for i in range(n)]


def binomial_band(n: int, p: float, sigmas: float = 3.0) -> tuple[float, float]:
    mean = n * p
    sd = math.sqrt(n * p * (1 - p))
    return mean - sigmas * sd, mean + sigmas * sd


class TestBucket:
    def test_is_stable_across_calls(self) -> None:
        assert bucket_of("abc") == bucket_of("abc")

    def test_lands_in_the_unit_interval(self) -> None:
        for trace_id in trace_ids(200):
            assert 0.0 <= bucket_of(trace_id) < 1.0

    def test_the_salt_changes_the_bucket(self) -> None:
        assert bucket_of("abc", salt="rule-a") != bucket_of("abc", salt="rule-b")

    def test_does_not_depend_on_python_hash_randomisation(self) -> None:
        # The value is pinned, so a change of algorithm is a visible, deliberate change
        # rather than a silent re-sampling of every project's traffic. `hash()` would
        # differ per process and destroy reproducibility outright.
        assert bucket_of("trace-1") == pytest.approx(0.7864223755896091, abs=1e-12)


class TestSampleRate:
    @pytest.mark.parametrize("rate", [0.01, 0.05, 0.25, 0.5])
    def test_the_realised_rate_matches_the_configured_rate(self, rate: float) -> None:
        ids = trace_ids(10_000)
        hits = sum(1 for t in ids if is_sampled(t, rate))
        low, high = binomial_band(len(ids), rate)
        assert low <= hits <= high

    def test_rate_zero_samples_nothing_and_rate_one_samples_everything(self) -> None:
        ids = trace_ids(500)
        assert not any(is_sampled(t, 0.0) for t in ids)
        assert all(is_sampled(t, 1.0) for t in ids)

    def test_an_out_of_range_rate_is_clamped_not_fatal(self) -> None:
        # A misconfigured 1.5 obviously means "everything". Crashing a worker mid-batch
        # over it would turn a typo into an outage.
        assert is_sampled("abc", 1.5)
        assert not is_sampled("abc", -0.2)

    def test_membership_is_a_property_of_the_trace(self) -> None:
        # The point of hashing rather than randomising: replaying a backlog must not spend
        # money again, and two runs must be comparable.
        ids = trace_ids(1_000)
        first = {t for t in ids if is_sampled(t, 0.1)}
        second = {t for t in ids if is_sampled(t, 0.1)}
        assert first == second

    def test_raising_the_rate_only_adds_traces(self) -> None:
        # Monotonicity, which falls out of comparing against a fixed bucket. It matters
        # operationally: turning the rate up must not drop traces that were being
        # evaluated, or a rate change would silently create a coverage gap.
        ids = trace_ids(2_000)
        at_one = {t for t in ids if is_sampled(t, 0.01)}
        at_ten = {t for t in ids if is_sampled(t, 0.10)}
        assert at_one <= at_ten


class TestIndependentSamples:
    def test_two_rules_sample_different_traces(self) -> None:
        # The subtle one. With a shared salt, every rule at 1 % selects the *same* 1 %,
        # so 99 % of traffic is invisible to every judge in the project and the sampled
        # cohort is fixed forever. Salting by rule id makes the samples independent.
        ids = trace_ids(20_000)
        a = {t for t in ids if is_sampled(t, 0.01, salt="rule-a")}
        b = {t for t in ids if is_sampled(t, 0.01, salt="rule-b")}
        assert a
        assert b
        overlap = len(a & b) / len(a)
        # Independent 1 % samples overlap on about 1 % of each other, not 100 %.
        assert overlap < 0.10

    def test_a_shared_sample_group_selects_the_same_traces(self) -> None:
        # Opt-in, for when several judges must score the same traces so their scores are
        # comparable per trace rather than only in aggregate.
        first = SamplingRule(rule_id="a", sample_rate=0.05, sample_group="shared")
        second = SamplingRule(rule_id="b", sample_rate=0.05, sample_group="shared")
        ids = trace_ids(2_000)
        chosen = [
            (
                decide(trace_id=t, rule=first).evaluate,
                decide(trace_id=t, rule=second).evaluate,
            )
            for t in ids
        ]
        assert all(a == b for a, b in chosen)


class TestDecide:
    def test_a_deterministic_rule_evaluates_everything_for_free(self) -> None:
        rule = SamplingRule(rule_id="policy", deterministic=True, sample_rate=0.0)
        decisions = [decide(trace_id=t, rule=rule) for t in trace_ids(500)]
        assert all(d.evaluate for d in decisions)
        assert all(d.reason == "deterministic" for d in decisions)
        # Excluded from every budget: a cost cap must not throttle checks that cost
        # nothing.
        assert not any(d.costs_money for d in decisions)

    def test_a_disabled_rule_evaluates_nothing(self) -> None:
        rule = SamplingRule(rule_id="r", enabled=False, deterministic=True)
        assert not decide(trace_id="abc", rule=rule).evaluate

    def test_forced_beats_everything(self) -> None:
        # A human asking for a specific trace must always win, including over a disabled
        # sample rate — that is what makes "why was this one skipped?" answerable by
        # simply re-running it.
        rule = SamplingRule(rule_id="r", sample_rate=0.0)
        decision = decide(trace_id="abc", rule=rule, forced=True)
        assert decision.evaluate
        assert decision.reason == "forced"
        assert decision.costs_money

    def test_a_failed_trace_escalates_past_the_sample(self) -> None:
        rule = SamplingRule(rule_id="r", sample_rate=0.0, escalate_on_failure=True)
        decision = decide(trace_id="abc", rule=rule, trace_failed=True)
        assert decision.evaluate
        assert decision.reason == "escalated"

    def test_escalation_can_be_turned_off(self) -> None:
        rule = SamplingRule(rule_id="r", sample_rate=0.0, escalate_on_failure=False)
        assert not decide(trace_id="abc", rule=rule, trace_failed=True).evaluate

    def test_escalation_is_capped(self) -> None:
        # An incident produces an error spike, which without a cap produces a judge-call
        # spike and a surprise bill on the worst possible day.
        rule = SamplingRule(rule_id="r", sample_rate=0.0)
        budget = EscalationBudget(limit=3)
        decisions = [
            decide(trace_id=t, rule=rule, trace_failed=True, budget=budget) for t in trace_ids(50)
        ]
        assert sum(1 for d in decisions if d.evaluate) == 3
        assert budget.exhausted

    def test_a_capped_escalation_is_distinguishable_from_not_sampled(self) -> None:
        # Different problems: one says "raise the cap or fix the failures", the other says
        # "this trace simply fell outside a 1 % sample". Reporting both as "no score" would
        # hide the first behind the second.
        rule = SamplingRule(rule_id="r", sample_rate=0.0)
        budget = EscalationBudget(limit=0)
        capped = decide(trace_id="abc", rule=rule, trace_failed=True, budget=budget)
        missed = decide(trace_id="abc", rule=rule, trace_failed=False)
        assert capped.reason == "capped"
        assert missed.reason == "not_sampled"
        assert not capped.evaluate
        assert not missed.evaluate

    def test_escalation_budget_is_only_spent_on_traces_the_sample_missed(self) -> None:
        # Escalation is checked after sampling so an in-sample failure does not consume
        # budget it never needed. Otherwise a high failure rate would exhaust the cap on
        # traces that were going to be evaluated anyway.
        rule = SamplingRule(rule_id="r", sample_rate=1.0)
        budget = EscalationBudget(limit=1)
        for trace_id in trace_ids(20):
            assert (
                decide(trace_id=trace_id, rule=rule, trace_failed=True, budget=budget).reason
                == "sampled"
            )
        assert budget.spent == 0

    def test_the_bucket_is_reported_for_a_sampling_decision(self) -> None:
        rule = SamplingRule(rule_id="r", sample_rate=0.5)
        decision = decide(trace_id="abc", rule=rule)
        assert decision.bucket is not None
        assert 0.0 <= decision.bucket < 1.0

    def test_no_bucket_is_reported_for_a_deterministic_rule(self) -> None:
        # Reporting one would imply the rule might sample, which it never does.
        rule = SamplingRule(rule_id="r", deterministic=True)
        assert decide(trace_id="abc", rule=rule).bucket is None


class TestCostEstimate:
    def test_counts_the_sample(self) -> None:
        assert expected_paid_calls(10_000, 0.01) == pytest.approx(100.0)

    def test_does_not_double_count_failures_inside_the_sample(self) -> None:
        # Escalation only fires for traces the sample missed, hence the (1 - rate) factor.
        # Ignoring it overstates cost by the failure rate — which sounds small until a
        # budget is sized from it.
        estimate = expected_paid_calls(10_000, 0.10, failure_rate=0.05)
        assert estimate == pytest.approx(1_000 + 10_000 * 0.05 * 0.9)

    def test_a_full_sample_leaves_nothing_to_escalate(self) -> None:
        assert expected_paid_calls(1_000, 1.0, failure_rate=0.5) == pytest.approx(1_000.0)

    def test_matches_reality_on_a_simulated_batch(self) -> None:
        # The estimate is what a user sizes a budget from, so it is worth checking against
        # the code that actually makes the decisions rather than only against arithmetic.
        rule = SamplingRule(rule_id="r", sample_rate=0.10)
        ids = trace_ids(10_000)
        # A deterministic 5 % of traces "fail", chosen independently of the sample salt.
        failed = {t for t in ids if bucket_of(t, salt="failure") < 0.05}
        paid = sum(
            1 for t in ids if decide(trace_id=t, rule=rule, trace_failed=t in failed).costs_money
        )
        estimate = expected_paid_calls(len(ids), 0.10, failure_rate=len(failed) / len(ids))
        assert abs(paid - estimate) < 0.1 * estimate
