"""Adversarial fix (HIGH #7): correct robots.txt path matching (RFC 9309).

The previous implementation used naive ``str.startswith(pattern)`` which:
- ignored ``*`` wildcards (``Disallow: /private/*`` matched nothing),
- ignored ``$`` end anchors (``Disallow: /page$`` matched ``/page/sub``),
- applied empty-Disallow semantics incorrectly (did not override a prior
  ``Disallow: /``).

These tests pin the correct RFC 9309 behavior.
"""

from app.crawl.engine import _robots_allows


def test_wildcard_disallow_blocks_subpaths():
    rules = [(False, "/private/*")]
    assert _robots_allows("http://x.example/private/secret", rules) is False
    assert _robots_allows("http://x.example/public", rules) is True


def test_dollar_anchor_is_end_anchored():
    rules = [(False, "/page$")]
    # Exactly /page is disallowed...
    assert _robots_allows("http://x.example/page", rules) is False
    # ...but /page/sub is NOT matched by the $ anchor.
    assert _robots_allows("http://x.example/page/sub", rules) is True


def test_empty_disallow_overrides_prior_disallow():
    # RFC 9309: an empty Disallow value permits everything and overrides a
    # prior broad Disallow.
    rules = [(False, "/"), (False, "")]
    assert _robots_allows("http://x.example/anything/at/all", rules) is True


def test_longest_matching_rule_wins():
    # Allow everything by default, but disallow *.json files.
    rules = [(True, "/"), (False, "/*.json")]
    assert _robots_allows("http://x.example/foo.json", rules) is False
    assert _robots_allows("http://x.example/foo", rules) is True


def test_plain_prefix_disallow_still_works():
    rules = [(False, "/private")]
    assert _robots_allows("http://x.example/private", rules) is False
    assert _robots_allows("http://x.example/private/sub", rules) is False
    assert _robots_allows("http://x.example/public", rules) is True


def test_disallow_root_blocks_everything():
    rules = [(False, "/")]
    assert _robots_allows("http://x.example/anything", rules) is False
