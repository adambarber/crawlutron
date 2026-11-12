"""The GC-caller law for the cache maintenance seams: a reaping seam with zero production callers is dead
surface, so ``ContentCache.prune`` and the robots ``purge_robots_cache`` export must each have a live
production call site -- today the app-lifespan maintenance pass in ``crawlutron.service.cache_sweep``.
"""

from __future__ import annotations

import ast

from tests.chokepoint_walks import APP_ROOT
from tests.invariants._shared import REPO_ROOT

_SEAMS = ("prune", "purge_robots_cache")
_MAINTENANCE_MODULE = "packages/core/src/crawlutron/service/cache_sweep.py"


def _call_names(tree: ast.AST) -> set[str]:
    """Every name a call in ``tree`` invokes: bare names and attribute tails."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_the_maintenance_module_calls_both_gc_seams():
    tree = ast.parse((REPO_ROOT / _MAINTENANCE_MODULE).read_text(encoding="utf-8"))
    calls = _call_names(tree)
    for seam in _SEAMS:
        assert seam in calls, f"{_MAINTENANCE_MODULE} never calls {seam}()"


def test_no_other_production_call_site_is_required_to_exist():
    """The law is satisfied by the ONE maintenance chokepoint; prove it exists
    and is the only production caller, so a second divergent reaper cannot
    quietly appear without this test naming it."""
    callers: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = _call_names(tree)
        if any(seam in calls for seam in _SEAMS):
            callers.append(str(path.relative_to(REPO_ROOT)))
    assert callers == [_MAINTENANCE_MODULE]
