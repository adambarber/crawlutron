"""The ratchet funnel: what every budget gate in this suite does identically.

Four modules measure a budget over the app tree. Each supplies its own budgets
and its own collector, and each then re-typed the same five things: the
work-list printer, the not-exceeded assertion, the tightness assertion, the
determinism check, and the proof that its collector reaches real code. The
duplication gate caught that in the first place -- `test_metrics_budget.py` and
`test_typing_budget.py` cloned four regions of each other at 30 tokens a piece --
and a sanctioned annotation would have been the wrong answer. Five copies of one
ratchet is five places a future change to a ratchet has to be remembered, and
the fifth is the one nobody remembers.

So the funnel lives here and each gate supplies only what is genuinely its own:
the budget table, the collector, and the prose for its failure message. What
cannot be supplied -- the arithmetic, the assertion shapes, the whole-tree proof
-- is written once.

`Offender` is deliberately NOT here. The metrics gate's offender carries a size
and the typing gate's carries an annotation, and unifying them would mean every
offender carries two fields one of which is always meaningless. Each module keeps
its own and passes a `render` callable, which is the one line that has to know
the shape.

Every factory returns a named function rather than taking a decorator, because a
parametrized assertion has to be reachable by name at module scope for pytest to
collect it. `budget_not_exceeded(...)` is called at module level in the gate
module and its result assigned to `test_...`, which is why the `subject`
argument exists: it is what makes the two gates' failure messages
distinguishable in a parallel run.

The functions the factories build are deliberately NOT named `test_*`. This is a
helper module, not a test module, and a `def test_...` in here is never collected
by anything -- pytest only collects module-level names in a test module. The
hygiene invariant calls that out by name, and it is right: the name belongs to the
caller that binds it, and only the caller can put it where pytest will look.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tests.chokepoint_walks import APP_ROOT, app_trees, nodes_of

ALL_APP: frozenset[str] = frozenset()
"""Every app module.

Load-bearing, not decoration: `app_trees` excludes `app/safe_regex.py` by
default, and a gate that dropped a module would lower its own count and quietly
buy budget. It lives here rather than in each gate because two gates each
carrying the empty set and each re-arguing why it is empty is the argument
written twice and enforced neither time.
"""

Metrics = tuple[Callable[[str], int], Callable[[str], list[Any]]]
"""``(measurement, offenders)`` for one gate -- the two read functions.

Each gate keeps its own ``offenders``, because a work list is only useful in the
order its reader wants: the metrics gate sorts by size because the worst first is
the point, and the typing gate keeps tree order because a list of positions is a
list of edits. What is shared is the walk and the arithmetic around them, below.
"""


def walk(
    collect: Callable[[ast.AST], list[tuple[str, int, Any]]],
    *,
    exclude: frozenset[str] = ALL_APP,
) -> Iterator[tuple[str, str, Any]]:
    """Every candidate in the app tree, as ``(metric, where, detail)``.

    The collector is per-gate because what it measures is; the tree walk is not,
    because every gate wants the same one and every gate spells `relative` the
    same way. A collector that forgets to qualify its line numbers with a path
    produces a work list nobody can act on, so that is the part worth writing
    once.
    """
    for path, tree in app_trees(exclude=exclude):
        relative = path.relative_to(APP_ROOT.parent).as_posix()
        for metric, line, detail in collect(tree):
            yield metric, f"{relative}:{line}", detail


def definition_counts(*, exclude: frozenset[str] = ALL_APP) -> dict[str, int]:
    """How many functions and classes the walk reached, for the whole-tree proof."""
    functions = classes = 0
    for _path, tree in app_trees(exclude=exclude):
        for node in nodes_of(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions += 1
            elif isinstance(node, ast.ClassDef):
                classes += 1
    return {"function": functions, "class": classes}


def report(
    budgets: dict[str, int],
    read: Metrics,
    render: Callable[[str, str], Any],
    *,
    headline: Callable[[str, int, int], str],
) -> None:
    """Print every metric, its budget, and its offenders -- the work list.

    `headline` exists because the metrics gate prints a unit and the typing gate
    prints a slash, and because one of them has a metric whose budget is its own
    limit. Neither is worth a second copy of the loop; both are worth a callable.
    """
    measurement, offenders = read
    for metric, budget in budgets.items():
        found = offenders(metric)
        shown = measurement(metric)
        print(headline(metric, shown, budget))
        for item in found:
            print(f"    {item}")


def budget_not_exceeded(
    budgets: dict[str, int],
    read: Metrics,
    *,
    subject: str,
    remedy: str,
) -> Callable:
    """The gate: no metric may hold more than its budget allows."""
    measurement, offenders = read

    @pytest.mark.parametrize("metric", sorted(budgets))
    def assertion_not_exceeded(metric: str) -> None:
        budget = budgets[metric]
        found = offenders(metric)
        shown = measurement(metric)
        assert shown <= budget, (
            f"{subject}: {metric}: {shown} exceeds the budget of {budget}.\n"
            f"{remedy}\n" + "\n".join(f"  {item}" for item in found)
        )

    return assertion_not_exceeded


def budget_is_tight(budgets: dict[str, int], read: Metrics, *, subject: str) -> Callable:
    """A budget may not sit above the measurement, or it is a standing allowance.

    Without this the gate is satisfied by a number nobody ever lowers, and the
    first commit to raise a budget above the tree goes green instead of red.
    """
    measurement, _offenders = read

    @pytest.mark.parametrize("metric", sorted(budgets))
    def assertion_is_tight(metric: str) -> None:
        budget = budgets[metric]
        actual = measurement(metric)
        assert budget <= actual, (
            f"{subject}: {metric}: the budget of {budget} is stale -- the measurement is now "
            f"{actual}. Tighten it to {actual} in `_BUDGETS` in the same commit that reduced "
            f"them. A budget that outlives the work it was counting is not a ratchet."
        )

    return assertion_is_tight


def collection_is_deterministic(budgets: dict[str, int], read: Metrics, *, subject: str) -> Callable:
    """The numbers in the budgets came from somewhere; make that somewhere checkable."""
    measurement, offenders = read

    def assertion_is_deterministic() -> None:
        for metric in budgets:
            assert offenders(metric) == offenders(metric), f"{subject}: {metric} offenders not deterministic"
            assert measurement(metric) == measurement(metric), (
                f"{subject}: {metric} measurement not deterministic"
            )

    return assertion_is_deterministic


def collector_sees_the_whole_tree(
    counts: Callable[[], dict[str, int]],
    minimums: dict[str, int],
    *,
    subject: str,
) -> Callable:
    """The gate is only worth what it scans.

    An empty scan and a whole-tree scan both report zero, so a broken collector
    would otherwise turn a budget module into permanently-passing tests. Proved
    by reaching real code, never by counting offenders -- which is the mistake
    the zero-observation proof in `tests/invariants/` already made once.

    A gate may walk for several things (the metrics gate counts functions and
    classes, the typing gate counts annotations), so the counts are a mapping and
    the thresholds are per-key.
    """
    minimum_modules = 50

    def assertion_reaches_the_tree() -> None:
        total = sum(1 for _ in APP_ROOT.rglob("*.py"))
        assert total > minimum_modules, f"{subject}: expected a real app tree, found {total} files"
        reached = counts()
        for what, minimum in minimums.items():
            assert reached.get(what, 0) > minimum, (
                f"{subject}: {what} walk reached only {reached.get(what, 0)}"
            )

    return assertion_reaches_the_tree
