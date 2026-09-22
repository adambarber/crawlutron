"""The hand-rolled robots matcher must agree with the dialect it implements.

``_RobotsPathMatcher`` replaces ``re`` to kill catastrophic backtracking
(#1766/#1783), so the reference oracle is the regex translation IT claims to
implement -- ``*`` -> ``.*``, left-anchored, trailing ``$`` = end anchor --
run here over a tiny generated alphabet where backtracking is irrelevant.
One implementation (the "exactly one definition" invariant) is not one
CORRECT implementation: the anchored branch undermatched for any pattern
whose last literal also occurs earlier (``/*a$`` vs ``/aba`` -> False), a
fail-open in robots enforcement. The oracle catches that whole class, which
is why it is a parity test rather than a hand-picked table.
"""

import random
import re

import pytest

from app.crawl.engine import _RobotsPathMatcher


def _reference(pattern: str, path: str) -> bool:
    """Dialect reference: left-anchored regex, trailing $ pins the end."""
    anchored = pattern.endswith("$") and len(pattern) > 1
    body = pattern[:-1] if anchored else pattern
    regex = "^" + ".*".join(re.escape(part) for part in body.split("*")) + ("$" if anchored else "")
    return re.match(regex, path) is not None


@pytest.mark.parametrize("seed", range(20))
def test_matcher_matches_the_regex_dialect_on_generated_inputs(seed: int):
    rng = random.Random(seed)
    cases = 0
    while cases < 50:
        pattern = "/" + "".join(
            rng.choice(["a", "b", "*", ".", "$", "ab", "*a", "b*", "$"])
            for _ in range(rng.randint(0, 4))
        )
        if pattern.endswith("$") and len(pattern) > 1:
            pass  # valid anchor
        path = "/" + "".join(rng.choice(["a", "b", ".", "$", "ab", "ba"]) for _ in range(rng.randint(0, 6)))
        matcher = _RobotsPathMatcher(pattern)
        assert matcher.match(path) == _reference(pattern, path), (pattern, path)
        cases += 1


def test_anchored_pattern_matches_when_anchor_literal_also_occurs_earlier():
    """The shipped regression: greedy-leftmost + end-exact undermatched."""
    assert _RobotsPathMatcher("/*a$").match("/aba") is True
    assert _RobotsPathMatcher("/*.pdf$").match("/x.pdf/y.pdf") is True
    assert _RobotsPathMatcher("/*a$").match("/abb") is False  # does not end in a
    assert _RobotsPathMatcher("/foo$").match("/foo") is True
    assert _RobotsPathMatcher("/foo$").match("/foobar") is False
