"""Metrics ratchets: the RuboCop Metrics family, for a project Ruff does not cover.

Ruff owns the complexity half; the four measures below are BUDGETS, and
``largest class`` measures the worst class, not a count."""

from __future__ import annotations

import ast
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from scripts.ast_scopes import is_scope_opener
from scripts.jscpd_sweep import COMPOUND_NODES
from tests._ratchet import (
    budget_is_tight,
    budget_not_exceeded,
    collection_is_deterministic,
    collector_sees_the_whole_tree,
    definition_counts,
    walk,
)
from tests._ratchet import (
    report as ratchet_report,
)
from tests.chokepoint_walks import REPO_ROOT, app_trees, nodes_of, trees_under

# Every collector in this file reads the same app tree, and they are called once per metric per test that
# reads a number: the budget test reads both `offenders()` and `measurement()`, the tight test reads
_ALL_APP: frozenset[str] = frozenset()

FUNCTION_LENGTH = 60
ABC_SIZE = 30
BLOCK_LENGTH = 40

# What the tree is currently allowed to hold: offender counts for three metrics, and the size of the worst
# class for the fourth.
_BUDGETS = {
    # 22 -> 21 (#160); 21 -> 20 (#330); 20 -> 19 (#136); 19 -> 18 -> 17 -> 16 -> 15
    # (#419: from_mapping, render_request, run_async_crawl and _lifespan split).
    "function length": 15,
    "abc size": 0,
    # LOWERED 358 -> 357 (#383): BrowserRenderer's render loop lost a declaration line.
    # LOWERED 357 -> 355 (#136); 355 -> 348 (#419): the render phases left browser.py.
    "largest class": 348,
    # 8 first declared; 7 (#136-ish pool split); 7 -> 6 (#419, run_async_crawl's
    # guarded body split into claim/crawl/publish/failure phases).
    "block length": 6,
}

#: The scripts/ tree, outside this gate before #293; its measured baseline,
#: declared first (value-ratchet rule). ``largest class`` is zero: none declared.
_SCRIPTS_BUDGETS = {
    "function length": 2,
    "abc size": 1,
    "largest class": 0,
    "block length": 0,
}

# Branch vocabulary: the shared scope vocabulary whole (except handlers branch
# too) plus the two that can appear without opening a block. No restatement.
_BRANCH_NODES = COMPOUND_NODES + (ast.Assert, ast.BoolOp)


@dataclass(frozen=True)
class Offender:
    metric: str
    where: str
    name: str
    size: int

    def __str__(self) -> str:
        return f"{self.metric}: {self.where} {self.name} ({self.size})"


def _span(node: ast.stmt, body: list[ast.stmt]) -> int:
    if not body:
        return 0
    return (body[-1].end_lineno or body[-1].lineno) - node.lineno + 1


def _abc_size(node: ast.AST) -> int:
    nodes = list(nodes_of(node))
    # `AnnAssign` counts. A function with thirty `x: int = ...` statements is not less complex than one with
    # thirty `x = ...`; the distinction is whether the author wrote a type, not whether the code branches.
    assignments = sum(1 for item in nodes if isinstance(item, (ast.Assign, ast.AnnAssign)))
    branches = sum(1 for item in nodes if isinstance(item, _BRANCH_NODES))
    conditions = sum(1 for item in nodes if isinstance(item, ast.comprehension))
    return round(math.sqrt(assignments**2 + branches**2 + conditions**2))


def _outer_blocks(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Oversized compound statements, counting each region once.

    A 50-line ``with`` inside a ``try`` is one region; a block is kept only
    when no claimed enclosing span contains it."""
    candidates: list[tuple[int, int, int, str]] = []
    for node in nodes_of(tree):
        if not is_scope_opener(node):
            continue
        body = list(node.body)
        if not body:
            continue
        end = body[-1].end_lineno or body[-1].lineno
        candidates.append((node.lineno, end, end - node.lineno + 1, type(node).__name__))
    candidates.sort(key=lambda item: (item[0], -item[1]))

    claimed: list[tuple[int, int]] = []
    kept: list[tuple[int, int, str]] = []
    for start, end, size, label in candidates:
        if any(low <= start and end <= high for low, high in claimed):
            continue
        if size > BLOCK_LENGTH:
            kept.append((start, size, label))
        claimed.append((start, end))
    return kept


# Where each metric stops counting. ``largest class`` has no such line: its budget IS its limit, so the gate
# and the work list cannot disagree about what counts as too big.
_METRIC_LIMITS = {
    "function length": FUNCTION_LENGTH,
    "abc size": ABC_SIZE,
    "block length": BLOCK_LENGTH,
}


def _candidates(tree: ast.AST) -> list[tuple[str, int, tuple[int, str]]]:
    """Every (metric, line, (size, label)) this tree offers, over-limit or not.

    Measuring first and filtering once keeps the per-node cases to a single
    expression; the line travels because method names repeat."""
    found: list[tuple[str, int, tuple[int, str]]] = []
    for node in nodes_of(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(("function length", node.lineno, (_span(node, node.body), node.name)))
            found.append(("abc size", node.lineno, (_abc_size(node), node.name)))
        elif isinstance(node, ast.ClassDef):
            found.append(("largest class", node.lineno, (_span(node, node.body), node.name)))
    for start, size, label in _outer_blocks(tree):
        found.append(("block length", start, (size, label)))
    return found


def _build_gate(candidates, trees, budgets):
    """(offenders, measurement) for one walk + budget pair, written once.
    The app and scripts halves differ only in tree and table; the work-list
    filter and worse-class scan are shared, not duplicated per root."""

    def offenders(metric: str) -> list[Offender]:
        limit = _METRIC_LIMITS.get(metric)
        budget = budgets[metric]
        found: list[Offender] = []
        for candidate_metric, where, detail in candidates():
            if candidate_metric != metric:
                continue
            size, label = detail
            if size > (budget if metric == "largest class" else limit):
                found.append(Offender(metric, where, label, size))
        found.sort(key=lambda item: (-item.size, item.where))
        return found[:1] if metric == "largest class" else found

    def measurement(metric: str) -> int:
        if metric != "largest class":
            return len(offenders(metric))
        worst = 0
        for _path, tree in trees():
            for node in nodes_of(tree):
                if isinstance(node, ast.ClassDef):
                    worst = max(worst, _span(node, node.body))
        return worst

    return offenders, measurement


offenders, measurement = _build_gate(
    lambda: walk(_candidates),
    lambda: app_trees(exclude=_ALL_APP),
    _BUDGETS,
)


def _root_candidates(root: Path) -> Iterator[tuple[str, str, tuple[int, str]]]:
    """Every candidate a whole root offers, as ``(metric, where, detail)``."""
    for path, tree in trees_under(root):
        relative = path.relative_to(REPO_ROOT).as_posix()
        for metric, line, detail in _candidates(tree):
            yield metric, f"{relative}:{line}", detail


def _root_gate(
    root: Path, budgets: dict[str, int]
) -> tuple[Callable[[str], list[Offender]], Callable[[str], int]]:
    """The offenders/measurement pair one root gate builds, written once."""
    candidates = lambda: _root_candidates(root)  # noqa: E731
    trees = lambda: trees_under(root)  # noqa: E731
    return _build_gate(candidates, trees, budgets)


scripts_offenders, scripts_measurement = _root_gate(REPO_ROOT / "scripts", _SCRIPTS_BUDGETS)


#: The tests/ tree had a file-size and comment-length budget but no complexity
#: ceiling; its measured baseline is declared first (value-ratchet rule).
_TESTS_BUDGETS = {
    "function length": 10,
    "abc size": 2,
    "largest class": 330,
    "block length": 1,
}

suite_offenders, suite_measurement = _root_gate(REPO_ROOT / "tests", _TESTS_BUDGETS)


def _assert_overlong_fn() -> None:
    """The collector fires on a synthetic over-long function (65 statements)."""
    assert any(
        m == "function length" and size > FUNCTION_LENGTH
        for m, _w, (size, _n) in _candidates(ast.parse("def runner():\n" + "    x = 1\n" * 65))
    ), "a 65-statement function must be an over-length offender"


def test_the_tests_budget_measurement_reaches_tests() -> None:
    """The H1 reach: the complexity ceiling now sweeps tests/, which had none.
    The real tree shows it (11 over-long functions are counted, not invisible) and
    the shared collector fires on a synthetic over-long fn in tests/."""
    assert suite_measurement("function length") == 10, (
        "the tests function-length budget stopped reaching the real tree"
    )
    assert suite_measurement("abc size") == 2
    assert suite_measurement("largest class") == 330
    _assert_overlong_fn()


def report() -> None:
    ratchet_report(
        _BUDGETS,
        (measurement, offenders),
        lambda metric, shown, budget: (
            f"{metric:<16} {shown:>3}{'' if metric == 'largest class' else ' offenders /':<12}"
            f" budget {budget:<3} [{'OVER' if shown > budget else 'ok'}]"
        ),
    )


_REMEDY = (
    "Each of these is work that was already on the list; a wave that fixes one "
    "lowers the budget in `_BUDGETS` in the same commit. The target is no "
    "offenders, at which point this assertion enforces the rule on its own."
)

test_budget_is_not_exceeded = budget_not_exceeded(
    _BUDGETS, (measurement, offenders), subject="metrics", remedy=_REMEDY
)
test_budget_is_tight = budget_is_tight(_BUDGETS, (measurement, offenders), subject="metrics")
test_collection_is_deterministic = collection_is_deterministic(
    _BUDGETS, (measurement, offenders), subject="metrics"
)


_SCRIPTS_REMEDY = (
    "scripts/ was outside the complexity budget before #293; its baseline is the "
    "two over-long functions it shipped with. Split them and lower this budget."
)


test_scripts_budget_is_not_exceeded = budget_not_exceeded(
    _SCRIPTS_BUDGETS,
    (scripts_measurement, scripts_offenders),
    subject="scripts metrics",
    remedy=_SCRIPTS_REMEDY,
)
test_scripts_budget_is_tight = budget_is_tight(
    _SCRIPTS_BUDGETS, (scripts_measurement, scripts_offenders), subject="scripts metrics"
)
test_scripts_collection_is_deterministic = collection_is_deterministic(
    _SCRIPTS_BUDGETS, (scripts_measurement, scripts_offenders), subject="scripts metrics"
)


#: The tests/ tie to the same ratchet pattern: the baseline is fixed at the first
#: declaration and only ever lowers (value-ratchet rule).
_TESTS_REMEDY = (
    "tests/ had no complexity ceiling before this cohort; its baseline is the 11 "
    "over-long functions, 2 over-ABC functions, 1 oversized block and the 330-line "
    "class it shipped with. Split them and lower this budget."
)


test_tests_budget_is_not_exceeded = budget_not_exceeded(
    _TESTS_BUDGETS,
    (suite_measurement, suite_offenders),
    subject="tests metrics",
    remedy=_TESTS_REMEDY,
)
test_tests_budget_is_tight = budget_is_tight(
    _TESTS_BUDGETS, (suite_measurement, suite_offenders), subject="tests metrics"
)
test_tests_collection_is_deterministic = collection_is_deterministic(
    _TESTS_BUDGETS, (suite_measurement, suite_offenders), subject="tests metrics"
)


def test_the_scripts_budget_measurement_reaches_scripts() -> None:
    """The H1 reach: the complexity budget now sweeps scripts/, which had none.
    The real tree shows it, and the collector fires on a synthetic over-long fn."""
    assert scripts_measurement("function length") == 2, (
        "the scripts function-length budget stopped reaching the real tree"
    )
    assert scripts_measurement("abc size") == 1
    assert scripts_measurement("largest class") == 0
    _assert_overlong_fn()


test_collector_sees_the_whole_tree = collector_sees_the_whole_tree(
    definition_counts, {"function": 500, "class": 50}, subject="metrics"
)


def test_metric_budgets_have_never_risen() -> None:
    """A raised budget satisfies the gate and the tight check.

    History lives in the hermetic ledger (``tests/_metrics_budget_ledger.py``),
    never a git invocation -- raising means rewriting it (#253).
    """
    from tests._metrics_budget_ledger import (
        _BUDGET_HISTORY,
        _SCRIPTS_BUDGET_HISTORY,
        _TESTS_BUDGET_HISTORY,
    )

    # (live table, ledger). Importing the ledger inside the gate means a missing
    # or unparseable fixture raises here -- the gate fails closed, never skips.
    _assert_ledger_agrees(_BUDGETS, _BUDGET_HISTORY, subject="app")
    _assert_ledger_agrees(_SCRIPTS_BUDGETS, _SCRIPTS_BUDGET_HISTORY, subject="scripts")
    _assert_ledger_agrees(_TESTS_BUDGETS, _TESTS_BUDGET_HISTORY, subject="tests")


def _assert_ledger_agrees(live: dict[str, int], ledger: dict[str, list[int]], *, subject: str) -> None:
    """Every live budget matches the ledger's current value, monotonic given.

    A missing record, a raised value, or a non-monotonic ledger all fail closed.
    """
    for metric, value in live.items():
        history = ledger.get(metric)
        assert history, (
            f"{subject} budget: {metric} has no record in the hermetic ledger. Add its "
            "first-declared value, or a gate with no record cannot be a ratchet."
        )
        assert history[-1] == value, (
            f"{subject} budget: {metric} is {value} in the code but {history[-1]} in the "
            "ledger. Lower (raise) the budget in the same commit that edits the ledger."
        )
        _assert_monotonic(metric, history, subject=subject)
    missing = set(ledger) - set(live)
    assert not missing, (
        f"{subject} budgets: ledger records {sorted(missing)} that the live table does not "
        "define. Delete the stale records."
    )


def _assert_monotonic(metric: str, history: list[int], *, subject: str) -> None:
    """Values never rise: each older entry is >= the next, oldest-first."""
    for older, newer in zip(history, history[1:], strict=False):
        assert older >= newer, (
            f"{subject} budget: {metric} rose from {newer} to {older} in the hermetic "
            "ledger. A budget may only fall; rewrite the offenders instead."
        )
