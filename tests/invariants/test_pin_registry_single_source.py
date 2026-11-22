"""Version pins live only in the registry; the tree carries no SHA literals."""

from __future__ import annotations

import pathlib
import re
import tokenize

import pytest

from tests._walk_primitives import SCAN_ROOTS
from tests.chokepoint_paths import REPO_ROOT

_SHA_LITERAL = re.compile(r"[0-9a-f]{40}\Z")


def _sha_literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """String tokens carrying a commit SHA, as (line, token)."""
    with path.open("rb") as stream:
        try:
            tokens = list(tokenize.tokenize(stream.readline))
        except tokenize.TokenError, SyntaxError, IndentationError:
            return []
    return [
        (token.start[0], token.string)
        for token in tokens
        if token.type == tokenize.STRING and _SHA_LITERAL.search(token.string)
    ]


def _scanned_files() -> list[pathlib.Path]:
    """One instance per scanned file, derived from SCAN_ROOTS."""
    files: list[pathlib.Path] = []
    for root in SCAN_ROOTS:
        files.extend(path for path in (REPO_ROOT / root).rglob("*.py") if path.is_file())
    return sorted(files)


@pytest.mark.parametrize("path", _scanned_files())
def test_no_commit_sha_literal_outside_registry(path: pathlib.Path) -> None:
    """A SHA literal in code duplicates the registry and can drift from it."""
    offenders = _sha_literals(path)
    assert not offenders, f"{path}: pin literal outside .github/versions.toml: {offenders}"
