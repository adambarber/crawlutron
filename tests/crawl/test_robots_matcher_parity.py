"""The hand-rolled robots matcher must agree with the dialect it implements.

``_RobotsPathMatcher`` replaces ``re`` to kill catastrophic backtracking,
so the reference oracle is the regex translation IT claims to
implement -- ``*`` -> ``.*``, left-anchored, trailing ``$`` = end anchor --
run here over a tiny generated alphabet where backtracking is irrelevant.
One implementation (the "exactly one definition" invariant) is not one
CORRECT implementation: the anchored branch undermatched for any pattern
whose last literal also occurs earlier (``/*a$`` vs ``/aba`` -> False), a
fail-open in robots enforcement. The oracle catches that whole class, which is
why it is a parity test rather than a hand-picked table.

The GROUP level needs the same treatment for a different reason. ``robots_allows``
picks a winner across a whole group's rules, and a group may mix an empty
``Disallow:`` with real paths -- an empty pattern has no octets, so RFC 9309
2.2.2 makes it a rule to ignore, never the most specific match. The
implementation instead pre-scanned the group for the empty form and returned
allow-all on sight, which ignored every sibling rule. Nothing caught it: the
pattern-level oracle below says nothing about group composition, and the two
hand-written cases that touched the empty form both asserted the wrong verdict
(one of them citing an RFC 9309 override rule that does not exist). A generated
oracle over group compositions is what closes that, the same way the pattern
oracle closed its class.
"""

import random
import re

import pytest

from app.orchestration.robots import _RobotsPathMatcher, robots_allows
from tests._policy_factories import canonical


def _reference_allows(rules, path: str) -> bool:
    """RFC 9309 2.2.2 group evaluation, stated in the spec's own terms.

    A rule matches when its pattern does; the most specific match is the one
    with the most octets; an allow beats a disallow of equal length; no match
    at all means allowed. A pattern of zero octets -- the ``Disallow:`` empty
    form -- matches nothing and can never be the most specific, so it drops out
    structurally. Nothing here inspects the group as a whole, which is the
    property the implementation was missing.
    """
    best_len = -1
    allowed = True
    for is_allow, pattern in rules:
        if not pattern or not _reference(pattern, path):
            continue
        plen = len(pattern)
        if plen > best_len:
            best_len, allowed = plen, is_allow
        elif plen == best_len and is_allow:
            allowed = True
    return allowed


def _allow_all_shortcut(rules) -> bool:
    """The removed behavior, kept here so the oracle is proved to catch it."""
    return any((not is_allow) and pattern == "" for is_allow, pattern in rules)


@pytest.mark.parametrize("seed", range(20))
def test_group_evaluation_matches_the_rfc_oracle_on_generated_compositions(seed: int):
    rng = random.Random(seed)
    checked = 0
    while checked < 200:
        # The empty form has to appear for the oracle to have anything to say, and
        # it has to appear ALONGSIDE real rules, which is the composition that
        # let a named path be allowed.
        rules = [(False, "")]
        for _ in range(rng.randint(0, 3)):
            is_allow = rng.random() < 0.5
            pattern = "/" + "".join(rng.choice(["a", "b", "*", "/"]) for _ in range(rng.randint(0, 3)))
            if rng.random() < 0.2:
                rules.append((is_allow, ""))
            rules.append((is_allow, pattern))
        generated = "/" + "".join(rng.choice(["a", "b", "/"]) for _ in range(rng.randint(0, 3)))
        # The oracle judges the matcher, so both must read the same text: the
        # ADMITTED path. Judging the raw generated string instead would fold
        # the normalizer's path rewrites (/b// -> /b) into the parity claim,
        # which this suite is not about.
        url = canonical(f"http://x.example{generated}")
        path = url.path or "/"

        expected = _reference_allows(rules, path)
        assert robots_allows(url, rules) is expected, (rules, path, expected)
        checked += 1


def test_the_group_oracle_catches_the_allow_all_shortcut_it_exists_to_replace():
    """Bites proof: the oracle must disagree with the behavior it replaced."""
    # A group that names a path AND carries the empty form. The reference denies
    # the path; the shortcut allowed everything.
    rules = [(False, ""), (False, "/a")]
    assert _reference_allows(rules, "/a/b") is False
    assert _allow_all_shortcut(rules) is True

    # A group whose only rule is the empty form: no match, so allowed -- and here
    # the shortcut agrees, which is why it looked harmless in isolation.
    assert _reference_allows([(False, "")], "/a/b") is True
    assert _allow_all_shortcut([(False, "")]) is True


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
            rng.choice(["a", "b", "*", ".", "$", "ab", "*a", "b*", "$"]) for _ in range(rng.randint(0, 4))
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
