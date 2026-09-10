"""Shared AST-walk helpers for the chokepoint invariant (drift) tests.

Each safety chokepoint -- safe_regex, bounded_io, BoundedTTLCache, SafeUrl,
env parsing -- is guarded by an invariant test that walks app/*.py, parses
every module, and flags a banned pattern. The file walk, the parse loop, the
safe_regex.py exemption, and the offender-label format are the same across
all of them; they live here so the enforcement tests share ONE traversal.
The detectors themselves stay next to the contract they pin (in the test
files); this module only removes the repeated ~10-line walk scaffold per
test that otherwise invites the exemptions and labels to drift apart.

Keeping this pure-stdlib means the static invariant tests do not pay the
heavy ``helpers`` -> engine -> settings import chain.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

TESTS_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
APP_ROOT = REPO_ROOT / "app"
STRATEGIES_DIR = APP_ROOT / "crawl" / "strategies"

# app/safe_regex.py is the sanctioned regex wrapper (stdlib `re` for
# literals; the third-party `regex` engine for bounded execution). It owns
# the banned primitives, so a full-tree walk excludes it. A test that must
# also exempt its own chokepoint module passes `exclude=SANCTIONED | {"..."}`.
SANCTIONED = frozenset({"safe_regex.py"})


def app_python_files(*, exclude: frozenset[str] = SANCTIONED) -> list[pathlib.Path]:
    """Every app/*.py module except the sanctioned chokepoint wrappers."""
    return sorted(p for p in APP_ROOT.rglob("*.py") if p.name not in exclude)


def app_trees(*, exclude: frozenset[str] = SANCTIONED) -> Iterator[tuple[pathlib.Path, ast.Module]]:
    """Yield ``(path, tree)`` for the same files ``app_python_files`` walks."""
    for path in app_python_files(exclude=exclude):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def trees_under(directory: pathlib.Path) -> Iterator[tuple[pathlib.Path, ast.Module]]:
    """Yield ``(path, tree)`` for every module under ``directory``."""
    for path in sorted(directory.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def file_tree(relative: str) -> tuple[pathlib.Path, ast.Module]:
    """Parse one module by its repo-relative path (e.g. app/api/webhooks.py)."""
    path = REPO_ROOT / relative
    return path, ast.parse(path.read_text(encoding="utf-8"))


def offender(path: pathlib.Path, lineno: int, detail: str = "") -> str:
    """Format an offender as ``app/pkg/mod.py:lineno detail`` for messages."""
    label = f"{path.relative_to(REPO_ROOT)}:{lineno}"
    return f"{label} {detail}".strip()
