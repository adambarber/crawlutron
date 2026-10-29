"""No comment or docstring may cite a review item, milestone or fix number: those
markers point at documents a future reader does not have, so each host sentence must
stand alone. Scoped to crawlutron/ and tests/ plus ``.env.example`` -- not historical dated records.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: (label, pattern). The first two are not dead patterns. Each pattern is kept because it is the shape that
#: actually occurs, and the composite forms below cover the other shapes rather than leaving them ungated.
FORBIDDEN = (
    ("milestone item", re.compile(r"\(M\d+\)")),
    ("review item", re.compile(r"\((?:p|H|h)\d+\)")),
    # A lettered item after a slash, optionally followed by a second one: the shape that was live in tests/
    # and invisible to the two patterns above.
    ("composite item", re.compile(r"\b[A-Z]\d+\s*/\s*[A-Z]\d*\b")),
    # The same reference written out in words rather than letters.
    ("option reference", re.compile(r"\bOption\s*\d+\s*/\s*Item\s*\d+")),
    ("tier number", re.compile(r"\bTier\s*\d")),
    ("fix number", re.compile(r"\bFix-\d")),
)

SCANNED = sorted(
    path
    for path in (
        [
            *ROOT.joinpath("crawlutron").rglob("*.py"),
            *ROOT.joinpath("tests").rglob("*.py"),
            ROOT / ".env.example",
        ]
    )
    if path.is_file()
)


def test_the_census_is_not_empty():
    """Guards the scan itself: an empty file list makes every case below vacuous."""
    assert len(SCANNED) > 100, f"scan found only {len(SCANNED)} files"


#: One sample per pattern, which is not a sample of content in the tree -- it is a proof that the pattern is a
#: live regex.
MUST_MATCH = {
    "milestone item": "(" + "M" + "1)",
    "review item": "(" + "H" + "3)",
    "composite item": "(" + "C" + "2" + "/" + "M" + "11)",
    "option reference": "Option " + "3" + " / Item " + "3",
    "tier number": "Tier " + "2" + " remediation",
    "fix number": "Fix" + "-" + "17",
}


@pytest.mark.parametrize("label", sorted(MUST_MATCH), ids=sorted(MUST_MATCH))
def test_each_pattern_is_a_live_regex(label):
    """A gate that cannot fail is not a gate. Prove each pattern can still fire."""
    pattern = dict(FORBIDDEN)[label]
    assert pattern.search(MUST_MATCH[label]), f"{label} pattern does not match its own sample"


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
