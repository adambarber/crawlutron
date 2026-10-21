"""Hermetic budget ledger for the metrics gates (#253).

Oldest-first history per metric; the test proves it monotonic against the
live tables, replacing the git-history walk that silently skipped.
"""

from __future__ import annotations

#: Oldest-first history per ``_BUDGETS`` metric (app/). The first entry is the
#: value at first declaration; a later entry is a lowering, never a raise.
_BUDGET_HISTORY: dict[str, list[int]] = {
    #: 22 -> 21 (#160, result constructors left results.py); 21 -> 20 (#330,
    #: the five-branch evidence chain in _classify_extracted_content became one table).
    "function length": [22, 21, 20],
    "abc size": [1],
    #: 358 -> 357 (#383): BrowserRenderer's render retry loop lost a declaration
    #: line while threading the request budget through its pin rule.
    "largest class": [358, 357],
    #: 8 first declared; lowered to 7 when the document pool's acquisition split
    #: its growth logic out, and one oversized block left the tree.
    "block length": [8, 7],
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
