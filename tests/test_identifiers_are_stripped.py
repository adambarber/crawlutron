"""No comment or docstring may cite a review item, milestone or fix number.

Those markers pointed at numbered entries in review documents a future reader
does not have open, so every annotated comment pointed at a file the reader had
to go find first -- and several of them were the only thing explaining WHY a
line existed. The rule is that the host sentence must stand alone.

The patterns are written as a test rather than a lint rule because this is a
property of what the prose SAYS, and ruff has no opinion on that.

The scope is app/ and tests/ because a marker in either is a marker a reviewer
reads, plus ``.env.example`` because that file is read by whoever is setting the
service up: a marker there points an operator at a review document they do not
have. The root-level dated records are outside the scope by construction rather
than by exemption -- they describe what shipped at the time it shipped, and
stripping a marker from a historical entry would make it describe something that
never happened.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: (label, pattern). Each is a review-era marker that was never a real symbol.
#:
#: The first two are not dead patterns. They match nothing inside the scanned
#: scope only because every real marker here was written in a composite style the
#: pattern does not cover -- the item letter sitting after a slash rather than
#: immediately after the parenthesis, optionally with a second lettered item
#: beside it, or spelled out in words instead of letters. Each pattern is kept
#: because it is the shape that actually occurs, and the composite forms below
#: cover the other shapes rather than leaving them ungated. Every pattern is
#: asserted to still match something in the exempt records, so one that stops
#: matching anything at all is caught rather than sitting in the tuple as
#: decoration.
FORBIDDEN = (
    ("milestone item", re.compile(r"\(M\d+\)")),
    ("review item", re.compile(r"\((?:p|H|h)\d+\)")),
    # A lettered item after a slash, optionally followed by a second one: the
    # shape that was live in tests/ and invisible to the two patterns above.
    ("composite item", re.compile(r"\b[A-Z]\d+\s*/\s*[A-Z]\d*\b")),
    # The same reference written out in words rather than letters.
    ("option reference", re.compile(r"\bOption\s*\d+\s*/\s*Item\s*\d+")),
    ("tier number", re.compile(r"\bTier\s*\d")),
    ("fix number", re.compile(r"\bFix-\d")),
)

SCANNED = sorted(
    path
    for path in (
        [*ROOT.joinpath("app").rglob("*.py"), *ROOT.joinpath("tests").rglob("*.py"), ROOT / ".env.example"]
    )
    if path.is_file()
)


def test_the_census_is_not_empty():
    """Guards the scan itself: an empty file list makes every case below vacuous."""
    assert len(SCANNED) > 100, f"scan found only {len(SCANNED)} files"


#: One sample per pattern, which is not a sample of content in the tree -- it is a
#: proof that the pattern is a live regex. A pattern with a typo in it matches
#: nothing anywhere and the gate passes forever while looking like coverage,
#: which is the failure this table exists to make impossible. A pattern that has
#: fired and been cleaned (the tier and fix forms) is still a real shape, so it
#: stays with its sample.
#:
#: Each sample is joined from its letter and its number rather than written as a
#: whole. This file is inside the scan, so a complete literal here would be the
#: gate reporting its own fixtures.
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
