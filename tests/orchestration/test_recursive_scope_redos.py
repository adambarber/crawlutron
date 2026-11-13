"""CRITICAL fix: recursive crawl scope filtering must use the safe compiled regex.

A malicious ``scope_regex`` like ``(a+)+$`` would hang the loop on every link;
the pattern is compiled once (rejecting pathological forms) and used in the loop.
"""

import re

from tests._crawl_runners import run_scoped_crawl
from tests._fetcher_factories import (
    LINKED_PAIR_LIMITS,
    crawled_results,
    crawled_urls,
    site_linking_to,
)
from tests._policy_factories import canonical

CATASTROPHIC = "(a+)+$"


async def test_recursive_scope_regex_uses_safe_compilation(monkeypatch):
    # If the recursive loop applied the pattern with re.search(scope_regex, link), this guard would trigger
    # (the old, ReDoS-prone code path).
    orig = re.search

    def _guarded(pattern, *args, **kwargs):
        assert pattern != CATASTROPHIC, "recursive loop used unsafe re.search with raw scope_regex"
        return orig(pattern, *args, **kwargs)

    monkeypatch.setattr(re, "search", _guarded)

    pages = site_linking_to("/a", "/b")
    # It genuinely matches /a (ends in "a") and not /b, so scope filtering now WORKS where the old
    # reject-on-shape guard silently dropped the filter.
    urls = set(await crawled_urls(pages, limits=LINKED_PAIR_LIMITS, policy={"scope_regex": CATASTROPHIC}))
    assert "http://example.com/a" in urls
    assert "http://example.com/b" not in urls


async def test_recursive_scope_regex_timeout_fails_closed(monkeypatch):
    # A pathological pattern that defeats the regex engine's optimizer must raise RegexTimeout, which the
    # scope filter treats as out-of-scope (fail-closed): the crawl completes, nothing hangs.
    from crawlutron.safety.safe_regex import RegexTimeout

    calls = {"n": 0}

    def _always_timeout(pattern, *args, **kwargs):
        calls["n"] += 1
        raise RegexTimeout("test-induced budget expiry")

    # The bounded scope check is deduped into safe_url.scope_match; match_bounded runs there;
    # patching that one seam now guards both the recursive loop and the sitemap-seed filter.
    monkeypatch.setattr("safe_url.scope_match.match_bounded", _always_timeout)
    pages = site_linking_to("/a", "/b")
    out = await crawled_results(pages, limits=LINKED_PAIR_LIMITS, policy={"scope_regex": r"^/keep/"})
    assert calls["n"] > 0, "bounded match never invoked"
    urls = {r.normalized_url for r in out["results"]}
    assert canonical("http://example.com/") in urls
    assert canonical("http://example.com/a") not in urls
    assert canonical("http://example.com/b") not in urls


async def test_recursive_scope_regex_still_filters_with_valid_pattern(monkeypatch):
    # Valid scope patterns must still filter links, now via the compiled pattern.
    urls = await run_scoped_crawl(r"example\.com/keep/")
    assert "http://example.com/keep/x" in urls
    assert "http://example.com/drop/y" not in urls
