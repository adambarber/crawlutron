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

from tests.chokepoint_walks import trees_under
from tests.rules import Bites, Registry, Rule, Silent, Trips


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
        with pytest.raises(AssertionError, match="no offender"):
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
        with pytest.raises(AssertionError, match="expected 1 offender"):
            rule.check_bites()

    def test_an_exact_trip_count_passes(self):
        rule = _urljoin_rule(find=_every_or, bite=lambda: Trips("urljoin(a, b) or a\n", count=1))
        rule.check_bites()

    def test_a_wrong_trip_count_is_reported(self):
        rule = _urljoin_rule(find=_every_or, bite=lambda: Trips("urljoin(a, b) or a\n", count=3))
        with pytest.raises(AssertionError, match="expected 3 offender"):
            rule.check_bites()

    def test_a_zero_trip_count_is_refused_rather_than_satisfied(self):
        """Zero has its own spelling: a source that must not fire belongs in clean."""
        rule = _urljoin_rule(find=_every_or, bite=lambda: Trips("nothing()\n", count=0))
        with pytest.raises(ValueError, match="belongs in clean"):
            rule.check_bites()

    def test_a_marker_and_a_count_can_be_required_together(self):
        """Both halves at once, which is what most of the migrated proofs actually assert.

        A shape wrong in two places -- a path written and a path unlinked -- is two
        findings, so ``Trips`` states the count and ``Bites`` states what the message
        has to say. Neither alone is the claim: with only the count, a detector whose
        offender text has decayed to a bare path passes; with only the marker, one
        that fires three times where two were wrong passes too. Nearly every bite
        test in the invariant suite asserts both, so a framework that can only
        express one of them forces the migration to drop half the proof.
        """
        rule = _urljoin_rule(
            find=_every_or,
            bite=lambda: Bites("a = urljoin(x, y) or x\nb = x or y\n", marker="an or", count=2),
        )
        rule.check_bites()

    def test_a_count_below_what_was_found_is_reported_with_the_marker_too(self):
        rule = _urljoin_rule(
            find=_every_or,
            bite=lambda: Bites("a = urljoin(x, y) or x\nb = x or y\n", marker="an or", count=3),
        )
        with pytest.raises(AssertionError, match="expected 3 offender"):
            rule.check_bites()

    def test_a_matching_count_with_a_missing_marker_is_still_reported(self):
        """The marker is checked against a real offender, not against the count.

        A count of 2 and a marker no offender carries is the combination the two
        checks exist to catch independently, and an implementation that only
        verified the marker against the first offender would miss it here.
        """
        rule = _urljoin_rule(
            find=_every_or,
            bite=lambda: Bites("a = urljoin(x, y) or x\nb = x or y\n", marker="no-such-marker", count=2),
        )
        with pytest.raises(AssertionError, match="no offender"):
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


class TestRuleScope:
    """A rule's tree is a property of the rule, not of whoever sweeps it.

    ``Registry.check_all_clean`` used to sweep ``app_trees()`` for every rule,
    which was harmless while every rule was about ``app/`` and became a silent
    no-op the moment one was not. A rule about the test tree -- no test may write
    into ``app/``, no test may import a sibling helper by bare name -- swept
    production code instead, found nothing, and reported green. It is the
    worst failure this suite has: not a false alarm, a rule enforcing nothing
    while looking like it enforces something.
    """

    @staticmethod
    def _planted(tmp_path: pathlib.Path, source: str) -> pathlib.Path:
        target = tmp_path / "x.py"
        target.write_text(source, encoding="utf-8")
        return target

    def test_a_rule_declares_the_tree_it_is_swept_over(self, tmp_path):
        self._planted(tmp_path, "def f(a, b):\n    return urljoin(a, b)\n")
        rule = _urljoin_rule(scope=lambda *, exclude: trees_under(tmp_path))
        with pytest.raises(AssertionError) as caught:
            Registry((rule,)).check_all_clean()
        assert "x.py" in str(caught.value)

    def test_the_scope_is_honoured_by_check_all_clean_and_not_only_by_scan(self, tmp_path):
        """The registry path is the one the migrated files actually use.

        ``Rule.scan`` and ``Registry.check_all_clean`` are separate callers of the
        same sweep, and fixing one of them would leave a rule green through the
        other. Both have to read the declared scope.
        """
        self._planted(tmp_path, "def f(a, b):\n    return urljoin(a, b)\n")
        rule = _urljoin_rule(scope=lambda *, exclude: trees_under(tmp_path))
        with pytest.raises(AssertionError):
            rule.scan()

    def test_a_violation_outside_the_declared_scope_is_not_reported(self, tmp_path):
        """The scope narrows the sweep; it does not widen it to everything.

        Without this the first two tests would also be satisfied by a sweep that
        reads every tree it can find: a rule scoped to an empty directory must
        find nothing in the file beside it, and that is the only thing that
        distinguishes "swept where it said" from "swept more".
        """
        self._planted(tmp_path, "def f(a, b):\n    return urljoin(a, b)\n")
        empty = tmp_path / "clean"
        empty.mkdir()
        Registry((_urljoin_rule(scope=lambda *, exclude: trees_under(empty)),)).check_all_clean()

    def test_the_exclude_set_reaches_the_scope_walker(self, tmp_path):
        """A scope that is handed an empty exclude set cannot honour one.

        The owner of a chokepoint is sanctioned by basename, and a scope that
        ignores the exclude set would sweep the owner it is meant to spare.
        """
        self._planted(tmp_path, "def f(a, b):\n    return urljoin(a, b)\n")
        seen: list[frozenset[str]] = []

        def recording(*, exclude):
            seen.append(exclude)
            return trees_under(tmp_path)

        rule = _urljoin_rule(scope=recording, excludes=frozenset({"x.py"}))
        rule.scan()
        assert seen == [frozenset({"x.py"})]


class TestAProofStatesWhichModuleItRunsIn:
    """A detector keyed on a file's path is two claims, and one path holds one.

    ``at`` exists because "this is correct" and "this is correct *in this file*"
    are different claims, and only the second is what a path comparison enforces.
    Both halves have to be run, which is why the location is per proof and why a
    rule that places its bite somewhere must also place a ``Silent`` elsewhere.
    """

    OWNER = pathlib.Path("/repo/app/owner.py")
    ELSEWHERE = pathlib.Path("/repo/app/crawl/other.py")
    SOURCE = "class R:\n    async def close(self):\n        self._browser = None\n"

    @staticmethod
    def _clears_somewhere(tree, path):
        return (
            ["cleared outside the chokepoint"]
            if any(isinstance(n, ast.Assign) for n in ast.walk(tree))
            else []
        )

    def _only_in_owner(self, tree, path):
        return self._clears_somewhere(tree, path) if path.name == "owner.py" else []

    def _rule(self, *, clean=None, **overrides):
        return Rule(
            name="path-keyed",
            find=self._only_in_owner,
            bite=lambda: Bites(self.SOURCE, "chokepoint", at=self.OWNER),
            clean=clean,
            **overrides,
        )

    def test_a_bite_proof_runs_where_it_says(self):
        self._rule(clean=lambda: (Silent(self.SOURCE, at=self.ELSEWHERE),)).check_bites()

    def test_proving_at_the_default_path_instead_does_not_count_as_the_second_half(self):
        """A ``Silent`` at the firing path proves the same thing twice.

        This is the mistake the requirement exists to catch: the rule looks fully
        proved while the ninety files the detector deliberately ignores — the whole
        content of the guard — have never been swept once.
        """
        with pytest.raises(TypeError, match="somewhere else"):
            self._rule(clean=lambda: (Silent(self.SOURCE, at=self.OWNER),))

    def test_a_path_keyed_rule_without_any_clean_proof_cannot_be_constructed(self):
        with pytest.raises(TypeError, match="somewhere else"):
            self._rule()

    def test_a_rule_that_places_nothing_is_not_required_to_have_a_second_half(self):
        """``at`` is what raises the bar, so a rule that never uses it is unaffected."""
        Rule(name="plain", find=self._only_in_owner, bite=lambda: Bites(self.SOURCE, "chokepoint"))

    def test_a_silent_that_fires_is_reported_rather_than_ignored(self):
        """The proof is run at the path it names, not merely counted as declared.

        A detector that fires everywhere, paired with a ``Silent`` naming a module
        it does not fire in, is the false-negative shape: the declaration looks
        complete and the second half was never actually run against anything.
        """
        with pytest.raises(AssertionError, match="must not fire"):
            Rule(
                name="path-keyed",
                find=self._clears_somewhere,
                bite=lambda: Bites(self.SOURCE, "chokepoint", at=pathlib.Path("/repo/app/other.py")),
                clean=lambda: (Silent(self.SOURCE, at=self.OWNER),),
            ).check_bites()

    def test_a_silent_is_not_flattened_into_the_lookalikes(self):
        """A lookalike promises "spared everywhere"; a ``Silent`` promises one module.

        Folding the two together would assert the stronger claim at every path the
        rule happens to run, which is a claim nobody proved.
        """
        rule = self._rule(clean=lambda: (Silent(self.SOURCE, at=self.ELSEWHERE), "x = 1"))
        assert rule.looksalikes() == ("x = 1",)

    def test_a_string_lookalike_still_runs_beside_a_silent(self):
        self._rule(
            clean=lambda: (Silent(self.SOURCE, at=self.ELSEWHERE), "x = 1"),
        ).check_bites()


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
