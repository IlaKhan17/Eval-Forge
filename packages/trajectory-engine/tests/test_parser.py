"""Parser and predicate tests.

Two themes: errors must point at a line, and the predicate evaluator must refuse
anything that is not a comparison.
"""

from __future__ import annotations

import pytest

from evalforge_trajectory import PolicyError, load_policy
from evalforge_trajectory.predicates import PredicateError, compile_predicate, evaluate

MINIMAL = "name: p\nrules:\n  - id: r\n    kind: forbidden_action\n    actions: [x]\n"


class TestParsing:
    def test_minimal_policy_loads(self) -> None:
        loaded = load_policy(MINIMAL)
        assert loaded.policy.name == "p"
        assert len(loaded.policy.rules) == 1

    def test_content_hash_is_stable_and_content_sensitive(self) -> None:
        assert load_policy(MINIMAL).content_hash == load_policy(MINIMAL).content_hash
        assert load_policy(MINIMAL).content_hash != load_policy(MINIMAL + "# c\n").content_hash

    def test_rule_lines_are_recorded(self) -> None:
        assert load_policy(MINIMAL).line_for("r") == 3

    def test_invalid_yaml_reports_a_position(self) -> None:
        with pytest.raises(PolicyError, match=r":\d+:\d+: invalid YAML"):
            load_policy("name: p\nrules:\n  - [unclosed\n")

    def test_non_mapping_is_rejected(self) -> None:
        with pytest.raises(PolicyError, match="must be a YAML mapping"):
            load_policy("- just\n- a\n- list\n")


class TestSemanticValidation:
    def test_unknown_kind_names_the_offender_and_suggests(self) -> None:
        source = "name: p\nrules:\n  - id: r\n    kind: forbiden_action\n    actions: [x]\n"
        with pytest.raises(PolicyError) as exc:
            load_policy(source)
        assert "unknown kind 'forbiden_action'" in str(exc.value)
        assert "Did you mean 'forbidden_action'" in str(exc.value)

    def test_missing_kind_lists_the_options(self) -> None:
        with pytest.raises(PolicyError, match="has no `kind`"):
            load_policy("name: p\nrules:\n  - id: r\n    actions: [x]\n")

    def test_duplicate_rule_ids_are_rejected(self) -> None:
        source = (
            "name: p\nrules:\n"
            "  - id: same\n    kind: forbidden_action\n    actions: [x]\n"
            "  - id: same\n    kind: forbidden_action\n    actions: [y]\n"
        )
        with pytest.raises(PolicyError, match="duplicate rule id"):
            load_policy(source)

    def test_unknown_field_is_rejected(self) -> None:
        """A typo'd field would otherwise be silently ignored."""
        source = "name: p\nrules:\n  - id: r\n    kind: limit\n    action: a\n    max_call: 3\n"
        with pytest.raises(PolicyError, match="max_call"):
            load_policy(source)

    def test_limit_without_a_bound_is_rejected(self) -> None:
        source = "name: p\nrules:\n  - id: r\n    kind: limit\n    action: a\n"
        with pytest.raises(PolicyError, match="neither max_calls nor min_calls"):
            load_policy(source)

    def test_unsatisfiable_no_loop_is_rejected(self) -> None:
        source = (
            "name: p\nrules:\n  - id: r\n    kind: no_loop\n    window: 3\n    min_repeats: 5\n"
        )
        with pytest.raises(PolicyError, match="can never fire"):
            load_policy(source)

    def test_conditional_without_a_when_is_rejected(self) -> None:
        source = "name: p\nrules:\n  - id: r\n    kind: conditional\n    forbid_actions: [x]\n"
        with pytest.raises(PolicyError, match="no `when` predicate"):
            load_policy(source)

    def test_ambiguous_aliases_are_rejected(self) -> None:
        source = (
            "name: p\naliases:\n  a: [raw]\n  b: [raw]\n"
            "rules:\n  - id: r\n    kind: forbidden_action\n    actions: [x]\n"
        )
        with pytest.raises(PolicyError, match="alias resolution must be unambiguous"):
            load_policy(source)

    def test_alias_chains_are_rejected(self) -> None:
        source = (
            "name: p\naliases:\n  a: [b]\n  b: [c]\n"
            "rules:\n  - id: r\n    kind: forbidden_action\n    actions: [x]\n"
        )
        with pytest.raises(PolicyError, match="alias chains are not supported"):
            load_policy(source)

    def test_a_bad_predicate_fails_at_load_time(self) -> None:
        """Not when a particular trace happens to arrive."""
        source = (
            "name: p\nrules:\n  - id: r\n    kind: final_state\n"
            "    require: __import__('os').system('rm -rf /')\n"
        )
        with pytest.raises(PolicyError, match="rule 'r'"):
            load_policy(source)


class TestPredicateSafety:
    """The predicate evaluator must refuse anything that is not a comparison."""

    @pytest.mark.parametrize(
        "source",
        [
            "__import__('os')",
            "().__class__.__bases__",
            "[x for x in range(10)]",
            "lambda: 1",
            "open('/etc/passwd')",
            "eval('1')",
            "exec('x=1')",
            "state.__dict__",
            "1 if True else 2",
            "f'{state}'",
            "{'a': 1}",
        ],
    )
    def test_dangerous_expressions_are_rejected(self, source: str) -> None:
        with pytest.raises(PredicateError):
            compile_predicate(source)

    def test_an_overlong_predicate_is_rejected(self) -> None:
        with pytest.raises(PredicateError, match="exceeds"):
            compile_predicate("a == 'x' and " * 100 + "b == 'y'")

    def test_unknown_function_is_rejected_with_the_allowed_list(self) -> None:
        with pytest.raises(PredicateError, match="Allowed:"):
            compile_predicate("dangerous(state)")


class TestPredicateEvaluation:
    def test_comparison(self) -> None:
        assert evaluate("metadata.n > 3", {"metadata": {"n": 5}})
        assert not evaluate("metadata.n > 3", {"metadata": {"n": 1}})

    def test_boolean_operators(self) -> None:
        ns = {"metadata": {"a": 1, "b": 2}}
        assert evaluate("metadata.a == 1 and metadata.b == 2", ns)
        assert evaluate("metadata.a == 9 or metadata.b == 2", ns)
        assert evaluate("not metadata.a == 9", ns)

    def test_membership(self) -> None:
        ns = {"args": {"to": "a@x.com"}, "metadata": {"blocked": ["a@x.com"]}}
        assert evaluate("args.to in metadata.blocked", ns)
        assert not evaluate("args.to not in metadata.blocked", ns)

    def test_missing_field_is_falsey_not_an_error(self) -> None:
        """Policies read optional metadata; absence must not break evaluation."""
        assert not evaluate("metadata.absent == 'x'", {"metadata": {}})
        assert not evaluate("metadata.absent > 5", {"metadata": {}})
        assert evaluate("metadata.absent != 'x'", {"metadata": {}})

    def test_membership_in_a_missing_collection_is_false(self) -> None:
        assert not evaluate("args.to in metadata.missing", {"args": {"to": "a"}, "metadata": {}})

    def test_allowed_functions(self) -> None:
        ns = {"args": {"body": "Hello World", "items": [1, 2, 3]}}
        assert evaluate("len(args.items) == 3", ns)
        assert evaluate("startswith(args.body, 'Hello')", ns)
        assert evaluate("lower(args.body) == 'hello world'", ns)
        assert evaluate("exists(args.body)", ns)

    def test_chained_comparison(self) -> None:
        assert evaluate("0 < metadata.n < 10", {"metadata": {"n": 5}})
        assert not evaluate("0 < metadata.n < 10", {"metadata": {"n": 50}})


class TestActionsNamespaceTrap:
    """A rule that can never fire is worse than no rule: it looks like protection.

    Action names contain dots, so `actions.gmail.send` parses as nested attribute
    access and silently resolves to nothing. This was a real bug in the shipped
    reference policy, caught only because the end-to-end test exercised it.
    """

    def test_attribute_form_is_rejected_at_load_time(self) -> None:
        source = (
            "name: p\nrules:\n  - id: r\n    kind: required_action\n"
            "    action: validate\n    when: exists(actions.gmail.send)\n"
        )
        with pytest.raises(PolicyError, match="does not work"):
            load_policy(source)

    def test_the_error_shows_the_working_form(self) -> None:
        source = (
            "name: p\nrules:\n  - id: r\n    kind: required_action\n"
            "    action: validate\n    when: exists(actions.foo)\n"
        )
        with pytest.raises(PolicyError, match="in actions"):
            load_policy(source)

    def test_membership_form_is_accepted(self) -> None:
        source = (
            "name: p\nrules:\n  - id: r\n    kind: required_action\n"
            "    action: validate\n    when: \"'gmail.send' in actions\"\n"
        )
        assert load_policy(source).policy.rules[0].when == "'gmail.send' in actions"
