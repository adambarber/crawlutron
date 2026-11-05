"""The ratchet funnel: every budget gate supplies its own budgets, collector and
prose, and gets back the work-list, not-exceeded/tight/deterministic assertions
and a whole-tree proof -- written once, not five copies. ``Offender`` stays per-gate.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from typing import Protocol

import pytest

from tests.chokepoint_walks import APP_ROOT, app_trees, nodes_of

ALL_APP: frozenset[str] = frozenset()
"""Every app module.

Pins whole-tree scope at the gates that must see everything: if a default
exclusion ever returns to ``app_trees``, these gates keep their full count.
"""


class OffenderLike(Protocol):
    """One work-list entry: a position plus its rendering."""

    def __str__(self) -> str: ...


Metrics = tuple[Callable[[str], int], Callable[[str], list[OffenderLike]]]
"""``(measurement, offenders)`` for one gate -- the two read functions.

Each gate keeps its own ``offenders``, because a work list is only useful in the
order its reader wants: the metrics gate sorts by size because the worst first is
the point, and the typing gate keeps tree order because a list of positions is a
list of edits. What is shared is the walk and the arithmetic around them, below.
"""


def _read[T: OffenderLike](read: tuple[Callable[[str], int], Callable[[str], list[T]]]) -> Metrics:
    """Adapt one gate's own offender type to the shared entry protocol."""
    measurement, offenders = read

    def like_offenders(metric: str) -> list[OffenderLike]:
        return list(offenders(metric))

    return measurement, like_offenders


def walk[
    D,
](
    collect: Callable[[ast.AST], list[tuple[str, int, D]]],
    *,
    exclude: frozenset[str] = ALL_APP,
) -> Iterator[tuple[str, str, D]]:
    """Every candidate in the app tree as ``(metric, where, detail)``: the collector
    is per-gate but the walk is shared, and the part worth writing once is qualifying
    each line number with its path so the work list can be acted on.
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
    *,
    headline: Callable[[str, int, int], str],
) -> None:
    """Print every metric, its budget, and its offenders -- the work list.

    ``headline`` exists because the metrics gate prints a unit and the typing gate
    prints a slash; neither is worth a second loop, both are worth a callable.
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
) -> Callable[[str], None]:
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


def budget_is_tight(budgets: dict[str, int], read: Metrics, *, subject: str) -> Callable[[str], None]:
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


def collection_is_deterministic(
    budgets: dict[str, int], read: Metrics, *, subject: str
) -> Callable[[], None]:
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
) -> Callable[[], None]:
    """The gate is only worth what it scans: an empty and a whole-tree scan both
    report zero, so reach is proved against real code, never by counting offenders;
    a gate may walk for several things, so counts are a mapping with per-key minimums.
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
