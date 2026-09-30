"""No comment or docstring may cite a review item, milestone or fix number.

Those markers pointed at numbered entries in review documents a future reader
does not have open, so every annotated comment pointed at a file the reader had
to go find first -- and several of them were the only thing explaining WHY a
line existed. The rule is that the host sentence must stand alone.

The patterns are written as a test rather than a lint rule because this is a
property of what the prose SAYS, and ruff has no opinion on that. It is checked
over app/ and tests/ because a marker in either is a marker a reviewer reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: (label, pattern). Each is a review-era marker that was never a real symbol.
FORBIDDEN = (
    ("milestone item", re.compile(r"\(M\d+\)")),
    ("review item", re.compile(r"\((?:p|H|h)\d+\)")),
    ("tier number", re.compile(r"\bTier\s*\d")),
    ("fix number", re.compile(r"\bFix-\d")),
)

SCANNED = sorted([*ROOT.joinpath("app").rglob("*.py"), *ROOT.joinpath("tests").rglob("*.py")])


def test_the_census_is_not_empty():
    """Guards the scan itself: an empty file list makes every case below vacuous."""
    assert len(SCANNED) > 100, f"scan found only {len(SCANNED)} files"


@pytest.mark.parametrize("label,pattern", FORBIDDEN, ids=[label for label, _ in FORBIDDEN])
def test_no_review_identifier_survives_in_prose(label, pattern):
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()[:100]}"
        for path in SCANNED
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, (
        f"{label} markers reference a review document the reader does not have:\n" + "\n".join(offenders)
    )
