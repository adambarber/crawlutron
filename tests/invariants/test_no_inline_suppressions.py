"""The zero-suppression law: the tree carries NO inline suppressor, anywhere."""

from __future__ import annotations

import pathlib
import re
import tokenize

from tests._walk_primitives import scan_findings

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


def _suppression_findings(path: pathlib.Path) -> list[tuple[int, str]]:
    """Directive-bearing comments as (line, text) for the one shared finding door."""
    return [(line, comment) for line, comment in _comment_lines(path) if _DIRECTIVE.search(comment)]


def test_no_inline_suppression_anywhere() -> None:
    """One loop reports every file:line suppressor, so no failure hides another."""
    failures = scan_findings(_suppression_findings)
    assert not failures, "inline suppression banned by the zero-suppressor law:\n" + "\n".join(failures)


def test_detector_flags_a_planted_suppression_with_its_line(tmp_path: pathlib.Path) -> None:
    """Kills the mutant that drops comment tokens: a planted directive must be named."""
    planted = tmp_path / "planted.py"
    planted.write_text("x = 1  # noqa\n")
    assert _suppression_findings(planted) == [(1, "# noqa")]
