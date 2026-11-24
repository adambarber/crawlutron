"""Version pins live only in the registry; the tree carries no SHA literals."""

from __future__ import annotations

import pathlib
import re
import tokenize

from tests._walk_primitives import scan_findings

_SHA_LITERAL = re.compile(r"[0-9a-f]{40}\Z")


def _sha_literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """String tokens carrying a commit SHA, as (line, token)."""
    with path.open("rb") as stream:
        try:
            tokens = list(tokenize.tokenize(stream.readline))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            return []
    return [
        (token.start[0], token.string)
        for token in tokens
        if token.type == tokenize.STRING and _SHA_LITERAL.search(token.string)
    ]


def test_no_commit_sha_literal_outside_registry() -> None:
    """One loop reports every file:line pin literal, so no failure hides another."""
    failures = scan_findings(_sha_literals)
    assert not failures, "pin literal outside .github/versions.toml:\n" + "\n".join(failures)
