"""Hermetic budget ledger for the metrics gates (#253).

Oldest-first history per metric; the test proves it monotonic against the
live tables, replacing the git-history walk that silently skipped.
"""

from __future__ import annotations

#: Oldest-first history per ``_BUDGETS`` metric (app/). The first entry is the
#: value at first declaration; a later entry is a lowering, never a raise.
_BUDGET_HISTORY: dict[str, list[int]] = {
    #: 22 -> 21 (#160); 21 -> 20 (#330); 20 -> 19 (#136); then 19 -> 1 -> SEALED:
    #: the row AND this record are gone (#419's seal; the zero test is the gate).
    "abc size": [1, 0],
    #: 358 -> 357 (#383); 357 -> 355 (#136);
    #: 355 -> 348 (#419, the render phases left browser.py).
    "largest class": [358, 357, 355, 348],
    #: 8 first declared; 7 (#136-ish pool split); 6 -> 2 by #419's five splits;
    #: SEALED: row and record gone; test_zero_long_blocks_in_app is the gate.
}

#: Oldest-first history per ``_SCRIPTS_BUDGETS`` metric, declared at #293.
_SCRIPTS_BUDGET_HISTORY: dict[str, list[int]] = {
    #: fn-len 2 -> SEALED, abc 1 -> SEALED (#419: main and _run split; the rows
    #: AND these records are gone -- the zero assertions are the whole gates).
    "largest class": [0],
    "block length": [0],
}

#: Oldest-first history per ``_TESTS_BUDGETS`` metric, declared at #375.
_TESTS_BUDGET_HISTORY: dict[str, list[int]] = {
    #: 11 -> ... -> 5 -> 4 -> 3 (#419: ... registry tests, jscpd stub report).
    "function length": [11, 10, 9, 8, 7, 6, 5, 4, 3],
    #: 2 -> 0 (#419, same splits); the While went with the fixed-point walk.
    "abc size": [2, 0],
    "largest class": [330],
    #: 1 -> 0 (#419, the host-policy While moved into the fixed-point walk).
    "block length": [1, 0],
}

__all__ = [
    "_BUDGET_HISTORY",
    "_SCRIPTS_BUDGET_HISTORY",
    "_TESTS_BUDGET_HISTORY",
]
