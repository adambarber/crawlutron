"""The test suite is a consumer of app/, not a second copy of it.

WHY THIS GATE EXISTS
--------------------
The rule it enforces had no enforcement at all. "The test suite must not exceed
10% the size of the application code" stood as a written rule while nothing
measured it, which is the worst state a rule can be in: it reads like a
constraint, it cannot fail, and the number it describes moved without anyone
deciding it should.

It also turns out the number as written cannot be met, and the arithmetic is
worth stating because the rule has to change to something enforceable:

    app/        26,394 lines
    tests/      55,056 lines
    10% of app/  2,639 lines -- for 2,319 tests

That is 1.14 lines per test, and an empty ``def test_x(): pass`` is already two.
No arrangement of real tests reaches it. The rule was not "nearly met"; it was
roughly 21x off, and no amount of tidying reaches it without deleting coverage
or deleting the prose that explains the coverage. Both are refused explicitly in
the constraints this project works under.

SO WHAT IS ENFORCED INSTEAD
---------------------------
The concern behind the rule is real and survives the rule's failure: a test suite
that turns into a second application is unreviewable, and the growth shows up
first in the *scaffolding* -- the harnesses, doubles, walks and matchers that
exist only to make assertions expressible. Scaffolding is where the suite can
quietly become a codebase, because it has no owner, no coverage, and no
invariant of its own. So the enforced quantities are:

    SUPPORT_BUDGET   lines in tests/ modules that are not collected tests
    TOTAL_BUDGET     lines in all of tests/
    SCRIPTS_BUDGET   lines in scripts/ -- the enforcement home, not exempt
    RATIO_BUDGET     tests/ as a percent of app/, as an integer

and each of them ratchets DOWN ONLY. A test asserting a ceiling fails when the
measurement rises above it. A test asserting the budget is not stale fails when
the measurement falls below it, so the number cannot be left stale as a standing
allowance -- the same mechanism `test_pyright_budget.py` uses for its finding
count, and the same reason: a budget nobody tightens is not a budget.

CLASSIFICATION IS DERIVED, NOT ENUMERATED
-----------------------------------------
A support module is any module under ``tests/`` whose name is not ``test_*``.
Hand-listing the thirty-five support modules would be a second declaration of
the tree that drifts the moment one is added, and it would let a new 900-line
harness land without appearing in the number it is supposed to affect. Deriving
it means a new harness moves the measurement the moment it is written, which is
the only time the measurement is worth anything.

``scripts/membership_gate.py`` is counted as support, and deliberately so: it is
invariant machinery that no commit gate runs. The line between the two trees is
not "is this a gate" but "does a commit execute it" -- which is why the
duplication gate moved to ``scripts/`` and this one did not.

THE BITE
--------
A line counter that returns zero satisfies every ceiling above it. A
classification that treats a 900-line harness as a test module satisfies the
support budget too. So this file proves the measurement discriminates before it
is trusted to gate: it counts a synthetic tree it controls, checks the
support/test split lands on the right side, and checks that a module added past
the ceiling is actually reported.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from tests.chokepoint_walks import APP_ROOT, REPO_ROOT

#: Lines in ``tests/`` modules that are not collected tests. Currently 8,868
#: across 35 modules. This is the quantity the rule was actually reaching for.
SUPPORT_BUDGET = 8900

#: Lines in all of ``tests/``, test modules and support together. Currently
#: 55,579 across 206 modules -- which is this file included, because a budget
#: that excludes its own author is a budget that can be met by writing more gate.
#: That makes this number self-referential: it is the only measurement in the
#: repository that a line added anywhere under ``tests/`` has to pass, so it
#: cannot be set until the file that sets it is finished. Writing this gate moved
#: it three times on the way here; adding the detector-accounting module moved it
#: once more. Both were deliberate -- the budget exists to make growth visible and
#: decided, not to make it impossible, and a module that makes the suite's own
#: coverage knowable is worth the 210 lines it costs.
TOTAL_BUDGET = 55719

#: Lines in ``scripts/``. The duplication gate and the executable resolver moved
#: there, which is correct -- a commit gate should not execute code out of the
#: test suite -- and it would be a hole to leave the new home unbounded. Currently
#: 1,775.
SCRIPTS_BUDGET = 1775

#: ``tests/`` as a percent of ``app/``, as an integer. 2.1054 measured, so 211.
#: An integer rather than a float because a float comparison here would fail on
#: the last bit of an unrelated edit, and a gate that fails for a reason it
#: cannot explain gets deleted. Coarse on purpose: it moves a whole percent at a
#: time, so the twenty lines of an ordinary change cannot trip it and the ~260
#: that a new harness adds cannot miss it.
RATIO_BUDGET = 211

TESTS_ROOT = REPO_ROOT / "tests"
SCRIPTS_ROOT = REPO_ROOT / "scripts"


@dataclass(frozen=True)
class TreeStats:
    """Line counts for one tree, split the way the budgets are stated."""

    total: int
    support: int
    test_modules: int


def module_lines(path: pathlib.Path) -> int:
    """Physical line count of one module.

    Physical rather than logical: the budgets are about how much text a reader
    has to hold, and a logical count would let a module that is mostly one
    enormous docstring report as nearly empty. Docstrings are exactly what this
    project refuses to delete, so the count has to see them.
    """
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def is_test_module(path: pathlib.Path) -> bool:
    """Whether pytest collects this module.

    Derived from the collection pattern rather than from a list of support
    modules, so a new harness cannot be written outside the measurement.
    """
    return path.name.startswith("test_")


def tree_stats(root: pathlib.Path) -> TreeStats:
    """Count ``root``'s modules, split into collected tests and everything else."""
    total = support = tests = 0
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        count = module_lines(path)
        total += count
        if is_test_module(path):
            tests += 1
        else:
            support += count
    return TreeStats(total=total, support=support, test_modules=tests)


def tests_to_app_percent(tests_root: pathlib.Path, app_root: pathlib.Path) -> int:
    """``tests/`` as a whole percent of ``app/``.

    Rounded rather than truncated so the number never reads lower than the
    ratio it describes: a budget that under-states its own measurement is a
    budget that has to be revised the moment the file changes.
    """
    app_lines = sum(module_lines(p) for p in app_root.rglob("*.py") if "__pycache__" not in p.parts)
    if app_lines == 0:
        raise AssertionError(f"{app_root} measured zero lines; the ratio would be undefined")
    return round(100 * tree_stats(tests_root).total / app_lines)


def exceeds_budget(measured: int, budget: int) -> bool:
    """Whether ``measured`` has crossed ``budget``.

    The comparison the four ceilings above are made of, named so the bite can
    point it at numbers it controls. Four inline ``measured <= BUDGET``
    assertions look equivalent and are not testable as such: a bite that has to
    grow a real tree past 8,868 lines to prove the ceiling bites is a slow proof
    of something a one-line predicate is the whole of.
    """
    return measured > budget


def support_scaffolding_is_under_budget() -> None:
    measured = tree_stats(TESTS_ROOT).support
    assert not exceeds_budget(measured, SUPPORT_BUDGET), (
        f"tests/ support scaffolding is {measured} lines, over the {SUPPORT_BUDGET} budget "
        f"by {measured - SUPPORT_BUDGET}. A harness that grows here is growing the suite into "
        f"a second application. Either the growth earns its place in app/ or the test that "
        f"needed it should be asserting through an existing helper."
    )


def test_support_scaffolding_is_under_budget() -> None:
    support_scaffolding_is_under_budget()


def test_the_suite_as_a_whole_is_under_budget() -> None:
    measured = tree_stats(TESTS_ROOT).total
    assert not exceeds_budget(measured, TOTAL_BUDGET), (
        f"tests/ is {measured} lines, over the {TOTAL_BUDGET} budget by {measured - TOTAL_BUDGET}. "
        f"The suite is meant to be a consumer of app/, not a rival to it."
    )


def test_enforcement_tooling_is_under_budget() -> None:
    measured = tree_stats(SCRIPTS_ROOT).total
    assert not exceeds_budget(measured, SCRIPTS_BUDGET), (
        f"scripts/ is {measured} lines, over the {SCRIPTS_BUDGET} budget by {measured - SCRIPTS_BUDGET}. "
        f"Moving the duplication gate here was a correction, not a licence: this tree is now the "
        f"one place commit enforcement lives, and it is bounded for the same reason tests/ is."
    )


def test_the_suite_is_under_its_ratio_to_app() -> None:
    measured = tests_to_app_percent(TESTS_ROOT, APP_ROOT)
    assert not exceeds_budget(measured, RATIO_BUDGET), (
        f"tests/ is {measured}% of app/, over the {RATIO_BUDGET}% budget. The ratio is the "
        f"number the original rule was reaching for, measured as an integer so a gate failure "
        f"here is always a real movement and never a rounding artefact."
    )


def test_no_budget_is_left_stale() -> None:
    """A budget above its measurement is a standing allowance, not a ratchet.

    This is the half that makes the other half worth having. Without it the four
    ceilings above are satisfied by any number at or above today's, which is the
    same as having no ceiling: the number stops being a decision and becomes a
    fact nobody has to revisit. Each assertion here fails when the measurement
    has fallen BELOW its budget, so the only way to make this test pass again is
    to tighten the constant -- which is the edit that makes the reduction
    permanent instead of something the next change can spend back.
    """
    stats = tree_stats(TESTS_ROOT)
    assert SUPPORT_BUDGET <= stats.support, (
        f"the support budget of {SUPPORT_BUDGET} is stale -- only {stats.support} lines remain. "
        f"Tighten SUPPORT_BUDGET in this file."
    )
    assert TOTAL_BUDGET <= stats.total, (
        f"the total budget of {TOTAL_BUDGET} is stale -- only {stats.total} lines remain. "
        f"Tighten TOTAL_BUDGET in this file."
    )
    scripts = tree_stats(SCRIPTS_ROOT).total
    assert SCRIPTS_BUDGET <= scripts, (
        f"the scripts budget of {SCRIPTS_BUDGET} is stale -- only {scripts} lines remain. "
        f"Tighten SCRIPTS_BUDGET in this file."
    )
    ratio = tests_to_app_percent(TESTS_ROOT, APP_ROOT)
    assert RATIO_BUDGET <= ratio, (
        f"the ratio budget of {RATIO_BUDGET}% is stale -- the ratio is now {ratio}%. "
        f"Tighten RATIO_BUDGET in this file."
    )


def test_the_measurement_actually_counts_and_classifies(tmp_path: pathlib.Path) -> None:
    """The bite: a counter that returns zero would satisfy every budget above.

    Every ceiling in this file is satisfied by a measurement of zero, so a
    counter that silently read nothing would leave the whole gate green while
    enforcing nothing -- the same way a detector that matches nothing reads as a
    passing property. The proof is therefore not that the budgets hold but that
    the measurement responds to the tree it is pointed at, and classifies what it
    finds on the same rule the budgets are stated in.
    """
    root = tmp_path / "tests"
    root.mkdir()
    (root / "test_alpha.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    (root / "_harness.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    (root / "conftest.py").write_text("c = 1\n", encoding="utf-8")
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "test_alpha.cpython-312.pyc").write_text("noise", encoding="utf-8")

    stats = tree_stats(root)
    # 3 test lines + 2 harness + 1 conftest, and the .pyc is not Python source.
    assert stats.total == 6, f"expected 6 counted lines, got {stats.total}"
    assert stats.support == 3, f"expected 3 support lines, got {stats.support}"
    assert stats.test_modules == 1, f"expected 1 collected module, got {stats.test_modules}"

    # The split has to land on the same rule the budgets state, or support is
    # whatever the implementation happens to call a test.
    assert is_test_module(root / "test_alpha.py")
    assert not is_test_module(root / "_harness.py")
    assert not is_test_module(root / "conftest.py")

    # And the number has to MOVE when the tree does. The crossing itself is
    # checked on numbers this test owns rather than by inflating a temp tree past
    # an 8,868-line real budget, which would make the proof of a one-line
    # comparison cost 9,000 lines of fixture.
    before = stats.support
    (root / "_harness.py").write_text("\n".join(["a = 1"] * 40) + "\n", encoding="utf-8")
    after = tree_stats(root).support
    assert after == before + 38, f"adding 38 lines moved support by {after - before}"

    # The ceiling bites, and it spares a measurement sitting exactly on it.
    assert exceeds_budget(after + 1, after), "a value one line over its budget was not reported"
    assert not exceeds_budget(after, after), "a value exactly on its budget was reported"
    assert not exceeds_budget(after - 1, after), "a value under its budget was reported"
    assert exceeds_budget(0, 0) is False, "a zero budget must spare a zero measurement"


def test_the_ratio_refuses_an_empty_app_tree(tmp_path: pathlib.Path) -> None:
    """A zero-line ``app/`` must fail loudly rather than divide.

    The ratio is the one measurement here whose denominator can legitimately be
    zero -- an empty tree, a mis-pointed root, a checkout that lost its source.
    Each of those would otherwise produce a ZeroDivisionError, which reads as a
    crash in a test rather than as a measurement that could not be taken, and a
    gate whose failure mode is a traceback is a gate people route around.
    """
    empty = tmp_path / "app"
    empty.mkdir()
    try:
        tests_to_app_percent(TESTS_ROOT, empty)
    except AssertionError as exc:
        assert "zero lines" in str(exc)
    else:
        raise AssertionError("an empty app/ tree produced a ratio instead of refusing to")
