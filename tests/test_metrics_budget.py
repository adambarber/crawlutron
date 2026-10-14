"""Metrics ratchets: the RuboCop Metrics family, for a project Ruff does not cover.

Ruff owns the complexity half of this family and fails it outright at the
current worst (`C901`, `PLR0911`, `PLR0912`, `PLR0913`, `PLR0915`). It has no
equivalent for the four measures below, so they live here, in the project's
existing AST home, rather than in a second linter that would own one rule and
split the enforcement story across two exit codes.

    function length   body span in lines
    ABC size          sqrt(assignments^2 + branches^2 + conditions^2)
    largest class     body span of the single biggest class
    block length      span of one compound statement's body

Each is a BUDGET, not a threshold. A budget is the value the tree is currently
allowed to hold -- an offender count for three of them, the size of the worst
class for the fourth -- so this suite is green on the day it lands and every
later failure means the tree got worse. The offender list is in the failure
message, which makes the gate and the work list the same artifact -- there is
no separate report to fall out of date.

The target is zero in every case where zero means the property holds. For a
count that is "no offenders"; for ``largest class`` zero would mean "no classes
at all", which is not a thing a tree can reach by getting better. That metric's
zero is a tree with no class over the line, and the size of the worst class is
what lets a gate read that without counting what it is trying to eliminate.

Three of the four measure a COUNT. The fourth, ``largest class``, measures a
VALUE, and that is the amendment -- it replaced a count of classes over 250
lines, which could not see the one move the browser split made.

Counting offenders is blind to the only thing splitting is for. A 704-line
class divided into a 456-line renderer and a 550-line driver made the largest
thing in the tree 22% smaller and the offender count go UP by one, so the count
ratchet blocked the correct move and the only ways through were to leave the
class whole or to shred it into pieces small enough not to register. Neither is
a trade the metric was asked to make; it just could not tell them apart.

So the ratchet is now on the size of the worst single class, which is the thing
a reader actually has to hold in their head, and which fell from 704 to 550 in
that same commit. Its first recorded value is therefore LOWER than the worst the
count metric was implicitly tolerating, so the amendment is tighter than what it
replaces, not looser: any class growing past 550 fails, and the count metric
would have said nothing until a 251st line appeared in a fifth class.

What is given up: nothing stops a new oversized class appearing as long as the
biggest one does not grow. That is the blind spot a count had and this does not,
traded for the blind spot a count had and this does. A ratchet can watch one
thing well; watching two meant watching neither.

To see the current work list without failing anything, run this module and read
the counts, or call the collectors directly from a shell:

    python -c "import sys; sys.path[:0]=['tests']; \\
      import tests.test_metrics_budget as m; m.report()"

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

from scripts.ast_scopes import is_scope_opener
from scripts.executables import GIT, require_executable
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
from tests.chokepoint_walks import app_trees, nodes_of

# Every collector in this file reads the same app tree, and they are called once per metric per test that
# reads a number: the budget test reads both `offenders()` and `measurement()`, the tight test reads
_ALL_APP: frozenset[str] = frozenset()

FUNCTION_LENGTH = 60
ABC_SIZE = 30
BLOCK_LENGTH = 40

# What the tree is currently allowed to hold: offender counts for three metrics, and the size of the worst
# class for the fourth.
_BUDGETS = {
    "function length": 31,
    "abc size": 5,
    "largest class": 454,
    "block length": 11,
}

# The complexity approximation counts every node that introduces a branch, which is the sweep's own
# compound-statement vocabulary plus the three that can appear without opening a block.
_BRANCH_NODES = COMPOUND_NODES + (ast.ExceptHandler, ast.Assert, ast.BoolOp)


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

    A `try` that contains a 50-line `with` is one oversized region, not two, and
    an `if`/`elif`/`else` ladder is several. Reporting the inner spans too would
    inflate the count by measuring the same lines twice, which makes the budget
    meaningless. So a block is kept only when no already-claimed enclosing span
    contains it: outermost first, and nesting never double-counts.
    """
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
    expression each, instead of a repeated collect-and-append per metric. The
    line travels with the candidate because method names repeat -- every class
    in the tree has an `__init__` -- so a name is not an identity. The size and
    label are paired because `walk` owns the position and what remains to carry
    is the measurement itself.
    """
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


def offenders(metric: str) -> list[Offender]:
    """The work list for `metric`, worst first.

    For a counted metric that is every candidate over its limit. For
    ``largest class`` it is the single worst class, and only when it is worse
    than the budget -- so the list is never empty-but-over-budget, which is the
    state a report cannot render.
    """
    limit = _METRIC_LIMITS.get(metric)
    budget = _BUDGETS[metric]
    found: list[Offender] = []
    for candidate_metric, where, detail in walk(_candidates):
        if candidate_metric != metric:
            continue
        size, label = detail
        if size > (budget if metric == "largest class" else limit):
            found.append(Offender(metric, where, label, size))
    found.sort(key=lambda item: (-item.size, item.where))
    return found[:1] if metric == "largest class" else found


def measurement(metric: str) -> int:
    """The number `metric` is compared against.

    Counts for three metrics; for ``largest class`` the size of the worst class
    in the tree, whether or not it is over budget. Comparing a budget against
    ``len(offenders(...))`` would demand a budget of zero for that metric the
    moment the worst class came in under the line, which is the opposite of a
    ratchet.
    """
    if metric != "largest class":
        return len(offenders(metric))
    worst = 0
    for _path, tree in app_trees(exclude=_ALL_APP):
        for node in nodes_of(tree):
            if isinstance(node, ast.ClassDef):
                worst = max(worst, _span(node, node.body))
    return worst


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


test_collector_sees_the_whole_tree = collector_sees_the_whole_tree(
    definition_counts, {"function": 500, "class": 50}, subject="metrics"
)


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

    What this cannot police is a metric with too little history to compare: a
    budget introduced yesterday yields no adjacent-revision pair, so the loop
    below never runs for it and its first recorded value stands unchecked until
    a second point exists. `largest class` is in exactly that state. Its real
    protection is `test_metric_budget_is_tight` -- the budget cannot be above
    what the tree actually measures -- which is what makes this a disclosure
    rather than a fix: history is what catches a budget raised to hide a
    regression, and no amount of history helps a metric that has none yet.
    """
    service_root = pathlib.Path(__file__).resolve().parent.parent
    # Git paths are repo-root-relative, not service-relative.
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
        # `entries` is newest-first, so each adjacent pair runs (newer, older). the budget went up over time.
        for (newer, newer_value), (_, older) in zip(entries, entries[1:], strict=False):
            if newer_value > older:
                regressions.append(f"  {name}: rose from {older} to {newer_value} in {newer}")
    assert not regressions, (
        "a complexity budget rose in this repository's history. The rule is that "
        "these only fall; each wave lowers the number it reduced. Fix the offenders "
        "instead, or delete the entry that no longer applies.\n" + "\n".join(regressions)
    )
