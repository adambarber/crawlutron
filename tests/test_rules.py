"""The rule framework's own contract — mostly that a rule cannot be half-declared.

A framework that generates the tests can quietly become the thing that stops the
tests from existing: a rule with a detector that always returns nothing, or with
no proof at all, would produce a green suite and enforce nothing. That is the
failure this file exists to make impossible, so it tests the framework's
enforcement rather than the framework's mechanics.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.rules import Bites, Registry, Rule, Trips


def _finds_any_call(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A deliberately over-broad detector, for proving the proofs catch one."""
    return [f"{path}:{node.lineno}" for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _calls_urljoin(tree: ast.Module, path: pathlib.Path) -> list[str]:
    return [
        f"{path}:{node.lineno}: raw urljoin"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "urljoin"
    ]


def _every_or(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """A maximally over-eager detector: flags every ``or``, joined or not."""
    return [
        f"{path}:{node.lineno}: an or"
        for node in ast.walk(tree)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
    ]


def _urljoin_rule(**overrides) -> Rule:
    kwargs: dict = {
        "name": "no_raw_urljoin",
        "find": _calls_urljoin,
        "bite": lambda: "def f(a, b):\n    return urljoin(a, b)\n",
        "summary": "URLs are joined only through the chokepoint",
        "excludes": frozenset(),
    }
    kwargs.update(overrides)
    return Rule(**kwargs)


class TestBiteIsRequiredByConstruction:
    """The framework's central claim, tested rather than asserted in a docstring."""

    def test_a_rule_without_a_bite_cannot_be_constructed(self):
        with pytest.raises(TypeError):
            Rule(name="no_bite", find=_calls_urljoin)  # type: ignore[call-arg]

    def test_the_missing_argument_is_named(self):
        """A bare TypeError does not say which of the two required fields was absent."""
        with pytest.raises(TypeError, match="bite"):
            Rule(name="no_bite", find=_calls_urljoin)  # type: ignore[call-arg]

    def test_passing_bite_positionally_is_enough(self):
        rule = Rule("no_raw_urljoin", _calls_urljoin, lambda: "urljoin(a, b)\n")  # type: ignore[misc]
        assert rule.name == "no_raw_urljoin"


class TestBiteProof:
    def test_a_rule_whose_bite_does_not_fire_is_reported_as_enforcing_nothing(self):
        """The failure this framework exists to prevent, named in its own words."""
        dead = _urljoin_rule(bite=lambda: "def f():\n    return 1\n")
        with pytest.raises(AssertionError, match="enforcing nothing"):
            dead.check_bites()

    def test_a_bite_that_does_not_parse_is_a_broken_proof_not_a_broken_rule(self):
        with pytest.raises(AssertionError, match="does not parse"):
            _urljoin_rule(bite=lambda: "def f(:\n").check_bites()

    def test_a_working_rule_passes_its_own_proof(self):
        _urljoin_rule().check_bites()

    def test_every_declared_bite_must_fire_not_just_the_first(self):
        """A rule with one firing and one dead proof is a rule with a hole.

        Reporting only the first would let a later spelling of the same mistake
        go unwatched, which is exactly how the alias/root split in
        ``test_page_identity`` was missed in the first place.
        """
        half = _urljoin_rule(bite=lambda: ("urljoin(a, b)\n", "def f():\n    return 1\n"))
        with pytest.raises(AssertionError, match="did NOT fire"):
            half.check_bites()

    def test_a_single_bite_string_is_accepted_as_a_sequence_of_one(self):
        _urljoin_rule(bite=lambda: "urljoin(a, b)\n").check_bites()


class TestMarkerAndCountProofs:
    """``assert_detector`` proved three things; the rule must not prove fewer.

    A bare source only shows the detector fires. That is satisfied by a detector
    matching everything, and by one whose message has decayed to a generic
    string, so these two shapes carry the proof that the old one-call form did.
    """

    def test_a_marker_that_is_absent_from_the_offender_is_reported(self):
        rule = _urljoin_rule(bite=lambda: Bites("urljoin(a, b)\n", marker="safe_urljoin"))
        with pytest.raises(AssertionError, match="does not say"):
            rule.check_bites()

    def test_a_marker_that_is_present_passes(self):
        rule = _urljoin_rule(bite=lambda: Bites("urljoin(a, b)\n", marker="raw urljoin"))
        rule.check_bites()

    def test_a_detector_that_fires_twice_on_one_shape_is_reported(self):
        """The over-broad case, caught at the proof rather than in production.

        Two separate ``or`` expressions, not one chain: ``a or b or c`` is a single
        ``BoolOp`` with three values, so a detector that flags ``or`` rightly
        reports it once.
        """
        rule = _urljoin_rule(
            find=_every_or,
            bite=lambda: Bites("a = urljoin(x, y) or x\nb = x or y\n", marker="an or"),
        )
        with pytest.raises(AssertionError, match="exactly 1 offender"):
            rule.check_bites()

    def test_an_exact_trip_count_passes(self):
        rule = _urljoin_rule(find=_every_or, bite=lambda: Trips("urljoin(a, b) or a\n", count=1))
        rule.check_bites()

    def test_a_wrong_trip_count_is_reported(self):
        rule = _urljoin_rule(find=_every_or, bite=lambda: Trips("urljoin(a, b) or a\n", count=3))
        with pytest.raises(AssertionError, match="expected 3 offenders"):
            rule.check_bites()

    def test_a_zero_trip_count_is_refused_rather_than_satisfied(self):
        """Zero has its own spelling: a source that must not fire belongs in clean."""
        rule = _urljoin_rule(find=_every_or, bite=lambda: Trips("nothing()\n", count=0))
        with pytest.raises(ValueError, match="belongs in clean"):
            rule.check_bites()


class TestLookalikes:
    def test_a_lookalike_the_detector_flags_is_reported(self):
        """ "Flags innocent code" is a failure, not a warning.

        A rule that fires on everything it is shown passes its own bite and then
        reports every file in the repository, and the response to that is to
        delete the rule rather than fix it.
        """
        # A deliberately over-eager detector: every `or`, whatever it joins.
        # It passes a bite about `or` by construction, which is precisely why the
        # looksalike is the half that catches it.
        overeager = _urljoin_rule(find=_every_or, bite=lambda: "urljoin(a, b) or a\n")
        assert overeager.looksalikes() == ()  # no looksalikes declared yet

        with_lookalike = Rule(
            name="overeager",
            find=_every_or,
            bite=lambda: "urljoin(a, b) or a\n",
            clean=lambda: ["def f(a, b):\n    return a or b\n"],
        )
        with pytest.raises(AssertionError, match="lookalike"):
            with_lookalike.check_bites()

    def test_a_spared_lookalike_passes(self):
        rule = _urljoin_rule(clean=lambda: ("def f(a, b):\n    return safe(a, b)\n",))
        rule.check_bites()

    def test_a_rule_without_lookalikes_checks_none(self):
        _urljoin_rule().check_bites()


class TestCleanCheck:
    def _trees(self, source: str) -> list[tuple[pathlib.Path, ast.Module]]:
        return [(pathlib.Path("app/x.py"), ast.parse(source))]

    def test_a_tree_with_no_offenders_passes(self):
        _urljoin_rule().check_clean(self._trees("def f():\n    return 1\n"))

    def test_an_offending_tree_names_the_file_the_line_and_the_property(self):
        with pytest.raises(AssertionError) as caught:
            _urljoin_rule().check_clean(self._trees("def f(a, b):\n    return urljoin(a, b)\n"))
        message = str(caught.value)
        assert "app/x.py" in message
        assert "raw urljoin" in message
        assert "URLs are joined only through the chokepoint" in message

    def test_the_owner_is_excluded_by_default(self):
        """Sanctioned basenames keep a rule from failing on the owner that defines it."""
        rule = _urljoin_rule(excludes=frozenset({"validation.py"}))
        rule.check_clean([(pathlib.Path("app/validation.py"), ast.parse("urljoin(a, b)\n"))])


class TestRegistryReportsEveryFailure:
    """One failing rule must not hide the other twenty."""

    def test_every_violated_rule_is_reported_in_one_run(self):
        registry = Registry(
            (
                _urljoin_rule(name="dead_one", bite=lambda: "def f():\n    return 1\n"),
                _urljoin_rule(name="dead_two", bite=lambda: "def f():\n    return 1\n"),
            )
        )
        with pytest.raises(AssertionError, match="2 rule"):
            registry.check_all_bite()

    def test_a_healthy_registry_reports_nothing(self):
        Registry((_urljoin_rule(),)).check_all_bite()
