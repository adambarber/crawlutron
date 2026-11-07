"""The zero-suppression law: the tree carries NO inline suppressor, anywhere.

The law (AGENTS.md, owner directive 2026-11-05) bans every inline lint/type
suppression -- ``# noqa``, ``# type: ignore`` and friends -- in crawlutron/ AND
tests/: a checker complaint is resolved structurally or the code is rewritten.
This gate asserts that unconditionally. There is no budget, no exemption table
and no allowlist: at zero the only honest gate is the assertion itself.

The scan is token-precise: ``tokenize`` yields the real COMMENT tokens, so a
prose sentence that mentions a directive (like this docstring's) is not a hit,
and a ``#`` inside a string literal is not either.
"""

from __future__ import annotations

import pathlib
import re
import tokenize

import pytest

from tests._walk_primitives import REPO_ROOT

#: The source roots the law covers. scripts/ is checked by the same rules in
#: its own right (it imports like production code), docker/ ships its own tree.
_SCANNED_ROOTS = ("crawlutron", "tests")

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
    files: list[pathlib.Path] = []
    for root in _SCANNED_ROOTS:
        files.extend(p for p in (REPO_ROOT / root).rglob("*.py") if p.is_file())
    return sorted(files)


@pytest.mark.parametrize("path", _scanned_files())
def test_file_carries_no_inline_suppression(path: pathlib.Path) -> None:
    """Every comment token in every scanned file must be directive-free."""
    offenders = [(line, comment) for line, comment in _comment_lines(path) if _DIRECTIVE.search(comment)]
    assert not offenders, f"{path}: inline suppression banned by the zero-suppressor law: {offenders}"
