"""The extract leaf seal: extract imports only stdlib, internals, safe_url, pdfminer and lxml."""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

from tests.chokepoint_paths import REPO_ROOT

EXTRACT_ROOT = REPO_ROOT / "packages" / "extract" / "src" / "crawlutron_extract"

#: Permitted import roots: stdlib, the package itself, safe_url, pdfminer, lxml.
#: lxml is the owner-authorized strict XML parser (#1356); bs4 stays forbidden.
_ALLOWED = {
    "crawlutron",
    "crawlutron_extract",
    "safe_url",
    "pdfminer",
    "lxml",
}


def _is_forbidden(top: str) -> bool:
    """True if ``top`` is crawlutron (core wheel) or a third-party module
    other than safe_url, pdfminer and lxml."""
    top_level = top.split(".")[0]
    if top_level == "crawlutron":
        return True  # core wheel
    if top_level == "crawlutron_extract":
        return False  # internal
    if top_level in _ALLOWED or top_level in sys.stdlib_module_names:
        return False
    return True


def _module_imports(path: pathlib.Path) -> set[str]:
    """Return the set of top-level modules imported by a single source file."""
    imports: set[str] = set()
    source_code = path.read_text(encoding="utf-8")
    tree = ast.parse(source_code, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update({alias.name.split(".")[0] for alias in node.names})
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports


def _extract_modules() -> list[pathlib.Path]:
    """Every .py file under the crawlutron_extract package."""
    return sorted(p for p in EXTRACT_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_extract_module_reaches_crawlutron_or_third_party():
    """The seal: extract modules import only stdlib, internals, safe_url,
    pdfminer or lxml."""
    offenders: list[str] = []
    for path in _extract_modules():
        for top in sorted(_module_imports(path)):
            if _is_forbidden(top):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel} -> {top}")
    assert not offenders, (
        "crawlutron_extract reaches crawlutron or third-party (leaf violation):\n"
        + "\n".join(sorted(offenders))
    )


def _check_planted(planted: dict[str, set[str]], expected: str) -> None:
    """Run _is_forbidden over a planted import graph; assert expected offender."""
    offenders = set()
    for name, reached_set in planted.items():
        for reached in reached_set:
            if _is_forbidden(reached):
                offenders.add(f"{name} -> {reached}")
    assert expected in offenders


def test_extract_leaf_refuses_edge_to_crawlutron():
    """A planted crawlutron_extract -> crawlutron edge is an offender."""
    planted = {
        "crawlutron_extract.parser": {"crawlutron.extraction.content"},
        "crawlutron.extraction.content": set(),
    }
    _check_planted(planted, "crawlutron_extract.parser -> crawlutron.extraction.content")


def test_extract_leaf_refuses_third_party_edge():
    """A planted crawlutron_extract -> bs4 edge is an offender."""
    planted = {
        "crawlutron_extract.html_clean": {"bs4"},
        "bs4": set(),
    }
    _check_planted(planted, "crawlutron_extract.html_clean -> bs4")


def test_extract_leaf_every_module_is_scanned():
    """The seal must scan every crawlutron_extract module."""
    modules = _extract_modules()
    assert modules, "crawlutron_extract has no modules; seal would green vacuously"
    for path in modules:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.mark.parametrize("root", ["safe_url", "pdfminer", "lxml"])
def test_extract_leaf_allows_declared_dependency(root):
    """Each declared extract dependency is NOT an offender."""
    assert not _is_forbidden(root)
    assert not _is_forbidden(f"{root}.submodule")
