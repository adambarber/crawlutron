"""Commit-message citation gate: every commit must cite `#<number>`.

Stage commit-msg in .pre-commit-config.yaml; reads the message file passed as argv[1].
"""

from __future__ import annotations

import re
import sys

_CITATION = re.compile(r"#\d+\b")


def main(argv: list[str]) -> int:
    """Fail when the commit message names no issue."""
    if len(argv) != 1:
        print("usage: python -m scripts.commit_citation_gate <commit-msg-file>", file=sys.stderr)
        return 2
    try:
        message = open(argv[0], encoding="utf-8").read()
    except OSError as error:
        print(f"commit_citation_gate: cannot read {argv[0]}: {error}", file=sys.stderr)
        return 2
    if not _CITATION.search(message):
        print(
            f"commit_citation_gate: {argv[0]} cites no issue; add 'Closes #<number>' "
            "(repo rule: every commit cites an issue)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
