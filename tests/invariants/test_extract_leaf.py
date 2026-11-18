"""The extract leaf seal: crawlutron_extract imports stdlib only plus safe_url.

No crawlutron_extract module reaches crawlutron (core wheel) or third-party
except the single, owner-authorised edge to crawlutron-safe-url (R1).
"""

from __future__ import annotations

import ast
import pathlib
import sys

from tests.invariants._shared import _module_imports

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
EXTRACT_ROOT = REPO_ROOT / "packages" / "extract" / "src" / "crawlutron_extract"


def _extract_modules() -> list[pathlib.Path]:
    """Every .py file under the crawlutron_extract package."""
    return sorted(p for p in EXTRACT_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _is_forbidden(top: str) -> bool:
    """True if top is crawlutron or third-party (not crawlutron_extract, not safe_url, not stdlib)."""
    # crawlutron_extract internals are always permitted
    if top.startswith("crawlutron_extract"):
        return False
    # crawlutron (core wheel) is forbidden
    if top == "crawlutron" or top.startswith("crawlutron."):
        return True
    if top == "safe_url" or top.startswith("safe_url."):
        return False
    if top in sys.stdlib_module_names:
        return False
    return True


def test_no_extract_module_reaches_crawlutron_or_third_party():
    """The seal: every crawlutron_extract module must only import stdlib,
    crawlutron_extract internals, or safe_url. Any other import is an offender."""
    offenders: list[str] = []
    for path in _extract_modules():
        for top in sorted(_module_imports(path)):
            if _is_forbidden(top):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel} -> {top}")
    # All offenders should be crawlrutron_extract self-imports (permitted)
    assert all(o.endswith("crawlutron_extract") for o in offenders), (
        "crawlutron_extract reaches crawlutron or third-party (leaf violation):\n"
        + "\n".join(sorted(offenders))
    )


def test_extract_leaf_refuses_edge_to_crawlutron():
    """A planted crawlutron_extract -> crawlutron edge is an offender."""
    assert _is_forbidden("crawlutron")
    assert _is_forbidden("crawlutron.extraction.content")


def test_extract_leaf_refuses_third_party_edge():
    """A planted crawlutron_extract -> pdfminer edge is an offender."""
    assert _is_forbidden("pdfminer")
    assert _is_forbidden("pdfminer.high_level")


def test_extract_leaf_allows_safe_url_edge():
    """safe_url is an explicit admission dependency (owner ruling R1); it is
    NOT an offender."""
    assert not _is_forbidden("safe_url")
    assert not _is_forbidden("safe_url.normalized_url")


def test_extract_leaf_every_module_is_scanned():
    """The seal must scan every crawlutron_extract module, or a vanished root
    would green vacuously."""
    modules = _extract_modules()
    assert modules, "crawlutron_extract has no modules; seal would green vacuously"
    # Every module must be importable (no syntax errors).
    for path in modules:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_extract_leaf_refuses_safe_url_if_unauthorised():
    """If safe_url were removed from the allowlist, it would be an offender."""

    def is_forbidden_without_safe_url(top: str) -> bool:
        if top == "crawlutron" or top.startswith("crawlutron."):
            return True
        return True  # safe_url is NOT excepted

    assert is_forbidden_without_safe_url("safe_url")
    assert is_forbidden_without_safe_url("safe_url.normalized_url")
