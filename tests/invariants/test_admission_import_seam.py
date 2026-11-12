"""The admission kernel never imports crawlutron: it is a standalone library.

The import-contract gate (a rule) AST-scans every ``admission/**`` module and fails on
any crawlutron import, naming file:line.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterable

from tests.chokepoint_walks import APP_ROOT, REPO_ROOT, trees_under
from tests.rules import Bites, Rule, Silent

_ADMISSION = REPO_ROOT / "admission"
_SYNTHETIC = _ADMISSION / "_synthetic.py"
_ELSEWHERE = APP_ROOT / "crawl" / "content_cache.py"


def _crawlutron_import_offenders(tree: ast.Module, path: pathlib.Path) -> Iterable[str]:
    """Every crawlutron import/from-import in ``tree``, as a file:line offender.

    Path-keyed: the detector owns only ``admission/**``, so a crawlutron import
    elsewhere (the legitimate seam) stays quiet."""
    if _ADMISSION not in path.parents and path.parent != _ADMISSION:
        return []
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
            if any(name == "crawlutron" or name.startswith("crawlutron.") for name in names):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports crawlutron")
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "crawlutron" or node.module.startswith("crawlutron."):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} from-imports crawlutron")
    return offenders


ADMISSION_IMPORTS_NO_CRAWLUTRON = Rule(
    name="admission-imports-no-crawlutron",
    find=_crawlutron_import_offenders,
    summary=(
        "no admission/** module may import the crawler: the kernel is a "
        "standalone library with zero crawlutron coupling"
    ),
    scope=lambda *, exclude: trees_under(_ADMISSION),
    proof_path=_SYNTHETIC,
    bite=lambda: (
        Bites("import crawlutron\n\nX = 1\n", "imports crawlutron", at=_SYNTHETIC),
        Bites(
            "from crawlutron.models import normalized_url\n\nX = 1\n",
            "from-imports crawlutron",
            at=_SYNTHETIC,
        ),
        Bites("import crawlutron.runtime\n\nX = 1\n", "imports crawlutron", at=_SYNTHETIC),
    ),
    clean=lambda: (
        Silent("import crawlutron\n\nX = 1\n", at=_ELSEWHERE),
        Silent("import asyncio\n\nX = 1\n"),
        Silent("import admission\n\nX = 1\n"),
    ),
)

RULES = (ADMISSION_IMPORTS_NO_CRAWLUTRON,)


def test_admission_imports_no_crawlutron() -> None:
    """The kernel never imports the crawler (import-contract gate, §9.6)."""
    for rule in RULES:
        rule.scan()
        rule.check_bites()
