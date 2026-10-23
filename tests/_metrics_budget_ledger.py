"""Hermetic budget ledger for the metrics gates (#253).

Oldest-first history per metric; the test proves it monotonic against the
live tables, replacing the git-history walk that silently skipped.
"""

from __future__ import annotations

#: Oldest-first history per ``_BUDGETS`` metric (app/). The first entry is the
#: value at first declaration; a later entry is a lowering, never a raise.
_BUDGET_HISTORY: dict[str, list[int]] = {
    #: 22 -> 21 (#160); 21 -> 20 (#330); 20 -> 19 (#136);
    #: 19 -> 18 -> 17 -> 16 -> 15 (#419, four inline orchestration fns split).
    "function length": [22, 21, 20, 19, 18, 17, 16, 15],
    "abc size": [1, 0],
    #: 358 -> 357 (#383); 357 -> 355 (#136);
    #: 355 -> 348 (#419, the render phases left browser.py).
    "largest class": [358, 357, 355, 348],
    #: 8 first declared; 7 when the document pool's acquisition split its growth
    #: logic out; 6 (#419, run_async_crawl); 5 (#419, cffi_fetch stream extracted).
    "block length": [8, 7, 6, 5],
}

#: Oldest-first history per ``_SCRIPTS_BUDGETS`` metric, declared at #293.
_SCRIPTS_BUDGET_HISTORY: dict[str, list[int]] = {
    "function length": [2],
    "abc size": [1],
    "largest class": [0],
    "block length": [0],
}

#: Oldest-first history per ``_TESTS_BUDGETS`` metric, declared at #375.
_TESTS_BUDGET_HISTORY: dict[str, list[int]] = {
    #: 11 first declared; lowered to 10 by #253 when the git-history ratchet that
    #: occupied a 70-line function was reworked into a hermetic ledger.
    "function length": [11, 10],
    "abc size": [2],
    "largest class": [330],
    "block length": [1],
}

__all__ = [
    "_BUDGET_HISTORY",
    "_SCRIPTS_BUDGET_HISTORY",
    "_TESTS_BUDGET_HISTORY",
]
