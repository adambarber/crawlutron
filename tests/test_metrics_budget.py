"""Metrics ratchets: the RuboCop Metrics family, for a project Ruff does not cover.

Ruff owns the complexity half of this family and fails it outright at the
current worst (`C901`, `PLR0911`, `PLR0912`, `PLR0913`, `PLR0915`). It has no
equivalent for the four measures below, so they live here, in the project's
existing AST home, rather than in a second linter that would own one rule and
split the enforcement story across two exit codes.

    function length   body span in lines
    ABC size          sqrt(assignments^2 + branches^2 + conditions^2)
    class length      body span in lines
    block length      span of one compound statement's body

Each is a BUDGET, not a threshold. The budget is the number of offenders the
tree is currently allowed to have, so this suite is green on the day it lands
and every later failure means the tree got worse. The offender list is in the
failure message, which makes the gate and the work list the same artifact --
there is no separate report to fall out of date.

The target is zero in every case. At zero the budget does the failing on its
own, so the flip from "warn" to "enforce" is not a change to this file; it is
four integers in `_BUDGETS` reaching the value they are already heading for.
That is the whole reason this is a count and not a severity threshold: a
severity threshold never reaches zero on its own, and a gate that can be
satisfied forever is not a ratchet.

To see the current work list without failing anything, run this module and read
the counts, or call the collectors directly from a shell:

    python -c "import sys; sys.path[:0]=['tests']; \\
      import test_metrics_budget as m; m.report()"

Thresholds are deliberately aggressive. A ratchet set at the current worst is
a ratchet that has already failed; these sit low enough that the worst
functions in the tree are offenders, and low enough that the count moves
noticeably when a file is split.
"""

from __future__ import annotations

import ast
import math
import pathlib
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest
from _executables import GIT, require_executable
from _jscpd_sweep import COMPOUND_NODES
from chokepoint_walks import APP_ROOT, app_trees, nodes_of

# Every collector in this file reads the same app tree, and `offenders()` is
# called sixteen times per run (four metrics x the budget test, four x the
# tight test, four x two in the determinism test). It used to re-glob, re-read
# and re-parse all 106 modules on each of those, so one run paid for the same
# parse sixteen times over -- the exact redundancy a962c7b removed from the
# chokepoint suite, in the file next door. `app_trees` is that same owned
# snapshot, and `nodes_of` shares the walk underneath it.
#
# `exclude=frozenset()` is load-bearing, not decoration: the default excludes
# app/safe_regex.py, and these budgets are measured over EVERY app module. A
# dropped module would lower the offender count and quietly buy budget.
_ALL_APP = frozenset()

FUNCTION_LENGTH = 60
ABC_SIZE = 30
CLASS_LENGTH = 250
BLOCK_LENGTH = 40

# Offender counts the tree is currently allowed to have. These are the
# measurements at 0a87dd2, and they are the whole mechanism: a wave that
# splits a file lowers the number it is allowed to have, in the same commit
# that did the splitting. Lowering a budget below the true count fails the
# suite, so a budget cannot be quietly inflated -- the code has to actually get
# smaller first.
_BUDGETS = {
    "function length": 39,
    "abc size": 5,
    "class length": 3,
    "block length": 13,
}

# The complexity approximation counts every node that introduces a branch, which
# is the sweep's own compound-statement vocabulary plus the three that can
# appear without opening a block. Derived rather than re-spelled: the two lists
# agreeing is a fact nobody maintains, and the first time one of them gains a
# node the metrics gate silently stops counting a construct the duplication sweep
# is already measuring. ``ast.comprehension`` is counted separately below, as
# ``conditions``.
_BRANCH_NODES = COMPOUND_NODES + (ast.ExceptHandler, ast.Assert, ast.BoolOp)


@dataclass(frozen=True)
class Offender:
    metric: str
    where: str
    name: str
    size: int

    def __str__(self) -> str:
        return f"{self.metric}: {self.where} {self.name} ({self.size})"


def _span(node: ast.AST, body: list[ast.stmt]) -> int:
    if not body:
        return 0
    return (body[-1].end_lineno or body[-1].lineno) - node.lineno + 1


def _abc_size(node: ast.AST) -> int:
    nodes = list(nodes_of(node))
    # `AnnAssign` counts. A function with thirty `x: int = ...` statements is
    # not less complex than one with thirty `x = ...`; the distinction is
    # whether the author wrote a type, not whether the code branches. An earlier
    # version counted only `Assign`, so annotating every local scored ABC 0 --
    # the metric could be driven to zero by adding type hints, which is the
    # opposite of what a complexity budget is for.
    assignments = sum(1 for item in nodes if isinstance(item, (ast.Assign, ast.AnnAssign)))
    branches = sum(1 for item in nodes if isinstance(item, _BRANCH_NODES))
    conditions = sum(1 for item in nodes if isinstance(item, ast.comprehension))
    return round(math.sqrt(assignments**2 + branches**2 + conditions**2))


def _outer_blocks(tree: ast.AST) -> list[tuple[int, int, str]]:
    """Oversized compound statements, counting each region once.

    A `try` that contains a 50-line `with` is one oversized region, not two, and
    an `if`/`elif`/`else` ladder is several. Reporting the inner spans too would
    inflate the count by measuring the same lines twice, which makes the budget
    meaningless. So a block is kept only when no already-claimed enclosing span
    contains it: outermost first, and nesting never double-counts.
    """
    candidates: list[tuple[int, int, int, str]] = []
    for node in nodes_of(tree):
        if not isinstance(node, COMPOUND_NODES):
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


_METRIC_LIMITS = {
    "function length": FUNCTION_LENGTH,
    "abc size": ABC_SIZE,
    "class length": CLASS_LENGTH,
    "block length": BLOCK_LENGTH,
}


def _candidates(tree: ast.AST) -> list[tuple[str, int, str, int]]:
    """Every (metric, size, label, line) this tree offers, over-limit or not.

    Measuring first and filtering once keeps the per-node cases to a single
    expression each, instead of a repeated collect-and-append per metric. The
    line travels with the candidate because method names repeat -- every class
    in the tree has an `__init__` -- so a name is not an identity.
    """
    found: list[tuple[str, int, str, int]] = []
    for node in nodes_of(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(("function length", _span(node, node.body), node.name, node.lineno))
            found.append(("abc size", _abc_size(node), node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            found.append(("class length", _span(node, node.body), node.name, node.lineno))
    for start, size, label in _outer_blocks(tree):
        found.append(("block length", size, label, start))
    return found


def offenders(metric: str) -> list[Offender]:
    limit = _METRIC_LIMITS[metric]
    found: list[Offender] = []
    for path, tree in app_trees(exclude=_ALL_APP):
        relative = path.relative_to(APP_ROOT.parent).as_posix()
        for candidate_metric, size, label, line in _candidates(tree):
            if candidate_metric == metric and size > limit:
                found.append(Offender(metric, f"{relative}:{line}", label, size))
    return sorted(found, key=lambda item: (-item.size, item.where))


def report() -> None:
    for metric, budget in _BUDGETS.items():
        found = offenders(metric)
        state = "OVER" if len(found) > budget else "ok"
        print(f"{metric:<16} {len(found):>3} / budget {budget:<3} [{state}]")
        for item in found:
            print(f"    {item}")


@pytest.mark.parametrize("metric", sorted(_BUDGETS))
def test_metric_budget_is_not_exceeded(metric: str) -> None:
    budget = _BUDGETS[metric]
    found = offenders(metric)
    assert len(found) <= budget, (
        f"{metric}: {len(found)} offenders exceeds the budget of {budget}.\n"
        f"Each of these is work that was already on the list; a wave that fixes one "
        f"lowers the budget in `_BUDGETS` in the same commit. The target is 0, at which "
        f"point this assertion enforces the rule on its own.\n" + "\n".join(f"  {item}" for item in found)
    )


@pytest.mark.parametrize("metric", sorted(_BUDGETS))
def test_metric_budget_is_tight(metric: str) -> None:
    """A budget must not sit above the measurement, or it is a standing allowance."""
    budget = _BUDGETS[metric]
    actual = len(offenders(metric))
    assert budget <= actual, (
        f"{metric}: the budget of {budget} is stale -- only {actual} offenders remain. "
        f"Tighten it to {actual} in `_BUDGETS` in the same commit that reduced them. "
        f"A budget that outlives the work it was counting is not a ratchet."
    )


def test_metric_collectors_see_the_whole_tree() -> None:
    """The gate is only worth what it scans.

    An empty scan and a whole-tree scan both report zero offenders, so a broken
    collector would otherwise turn this module into four permanently-passing
    tests. This pins that the walkers reach real code rather than proving it by
    counting offenders, which is the mistake the zero-observation proof in
    `test_safety_invariants.py` already made once.
    """
    total = sum(1 for _ in APP_ROOT.rglob("*.py"))
    assert total > 50, f"expected a real app tree, found {total} files"

    functions = 0
    classes = 0
    for _path, tree in app_trees(exclude=_ALL_APP):
        for node in nodes_of(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions += 1
            elif isinstance(node, ast.ClassDef):
                classes += 1
    assert functions > 500, f"function walk reached only {functions} definitions"
    assert classes > 50, f"class walk reached only {classes} definitions"


def test_metric_collection_is_deterministic() -> None:
    """The numbers in the budgets came from somewhere; make that somewhere checkable."""
    for metric in _BUDGETS:
        assert offenders(metric) == offenders(metric), f"{metric} collection is not deterministic"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """One non-raising ``git`` call rooted at `root`, as decoded text.

    This test shells out three times and every one of them wants the same thing:
    text out, no raise, cwd pinned. Restated, the third call is the one that
    stopped checking ``returncode`` and started returning a ``stdout`` of "" that
    the loop below read as "no budget at this revision" -- a silently weakened
    history, not a failing one.
    """
    return subprocess.run(
        [require_executable(GIT), *args], cwd=root, capture_output=True, text=True, check=False
    )


def test_metric_budgets_have_never_risen() -> None:
    """The budget gate and the tight-budget check can both be satisfied at once by
    RAISING the budget alongside a new offender. That is not hypothetical: the
    per-file line budget in the safety suite was raised ten times in one day and
    lowered zero times, and the tight check could not see it because both
    assertions read the same table.

    A monotonicity test closes that: the budget history is read from git, so the
    only way to raise one is to edit history, which a reviewer sees. This is the
    one check here that needs a full clone; a shallow one cannot see the past and
    is skipped rather than passed, because a gate that silently goes inert is
    worse than one that is absent.
    """
    service_root = pathlib.Path(__file__).resolve().parent.parent
    # Git paths are repo-root-relative, not service-relative. Getting this wrong
    # makes every `git show` fail, `history` come back empty, and the whole
    # check pass vacuously -- which is exactly what the first version of this
    # test did. The `assert history` below is the guard against that class of
    # silent no-op ever recurring.
    root = pathlib.Path(
        subprocess.run(
            [require_executable(GIT), "rev-parse", "--show-toplevel"],
            cwd=service_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    tracked = str(pathlib.Path(__file__).resolve().relative_to(root))
    try:
        log = subprocess.run(
            [require_executable(GIT), "log", "--format=%H", "--", tracked],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        pytest.skip(f"git history unavailable: {error}")
    if not log:
        pytest.skip("no history for this file in this clone")

    shallow = _git(root, "rev-parse", "--is-shallow-repository")
    if shallow.stdout.strip() == "true" and len(log) < 5:
        pytest.skip("shallow clone: cannot see the budget's history")

    pattern = re.compile(r'^\s{4}"(?P<name>[a-z ]+)":\s*(?P<value>\d+),$')
    history: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for revision in log:
        source = _git(root, "show", f"{revision}:{tracked}")
        if source.returncode != 0:
            continue
        for line in source.stdout.splitlines():
            found = pattern.match(line)
            if found and found.group("name") in _BUDGETS:
                history[found.group("name")].append((revision[:9], int(found.group("value"))))

    assert history, (
        f"read {len(log)} revisions of {tracked} and found no budget values in any of "
        f"them. The parse pattern has drifted from the file it reads; a gate that "
        f"collects nothing must fail rather than pass."
    )
    regressions = []
    for name, entries in history.items():
        # `entries` is newest-first, so each adjacent pair runs (newer, older).
        # A regression is the newer value being HIGHER than the older one, i.e.
        # the budget went up over time. Note the direction: reading this as
        # (older, newer) inverts the test and makes a correctly falling ratchet
        # look like four regressions.
        for (newer, newer_value), (_, older) in zip(entries, entries[1:], strict=False):
            if newer_value > older:
                regressions.append(f"  {name}: rose from {older} to {newer_value} in {newer}")
    assert not regressions, (
        "a complexity budget rose in this repository's history. The rule is that "
        "these only fall; each wave lowers the number it reduced. Fix the offenders "
        "instead, or delete the entry that no longer applies.\n" + "\n".join(regressions)
    )
