"""The zero-suppression law: the tree carries NO inline suppressor, anywhere."""

from __future__ import annotations

import pathlib
import re
import tokenize

import pytest

from tests._walk_primitives import REPO_ROOT, SCAN_ROOTS

#: A directive is a comment that actually suppresses: noqa or a type-ignore
#: spelling. A backticked mention or an explanation is prose, never a hit.
_DIRECTIVE = re.compile(r"(?<!`)#\s*(?:noqa(?![\w`-])|type:\s*ignore(?![\w`-]))")


def _comment_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every (line number, comment text) the file's tokenizer reports."""
    with path.open("rb") as stream:
        try:
            tokens = list(tokenize.tokenize(stream.readline))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            return []
    return [(token.start[0], token.string) for token in tokens if token.type == tokenize.COMMENT]


def _scanned_files() -> list[pathlib.Path]:
    """One instance per scanned file, derived from SCAN_ROOTS directly -- no
    private root tuple, so a tree added later generates its own instances."""
    files: list[pathlib.Path] = []
    for root in SCAN_ROOTS:
        files.extend(p for p in (REPO_ROOT / root).rglob("*.py") if p.is_file())
    return sorted(files)


@pytest.mark.parametrize("path", _scanned_files())
def test_file_carries_no_inline_suppression(path: pathlib.Path) -> None:
    """Every comment token in every scanned file must be directive-free."""
    offenders = [(line, comment) for line, comment in _comment_lines(path) if _DIRECTIVE.search(comment)]
    assert not offenders, f"{path}: inline suppression banned by the zero-suppressor law: {offenders}"
