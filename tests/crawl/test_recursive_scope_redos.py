"""CRITICAL fix: recursive crawl scope filtering must use the safe
compiled regex, never a raw re.search(scope_regex, link).

A malicious scope_regex like (a+)+$ would hang the recursive loop via
catastrophic backtracking if applied with re.search on every discovered link.
The fix compiles the pattern once with compile_scope_pattern (which rejects
pathological patterns) and uses the compiled pattern's .search() in the loop.
"""

import re

from _fetcher_factories import run_scoped_crawl, site_fetcher

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request

CATASTROPHIC = "(a+)+$"


async def test_recursive_scope_regex_uses_safe_compilation(monkeypatch):
    # If the recursive loop applied the pattern with re.search(scope_regex, link),
    # this guard would trigger (the old, ReDoS-prone code path).
    orig = re.search

    def _guarded(pattern, *args, **kwargs):
        assert pattern != CATASTROPHIC, "recursive loop used unsafe re.search with raw scope_regex"
        return orig(pattern, *args, **kwargs)

    monkeypatch.setattr(re, "search", _guarded)

    pages = {
        "/": (
            200,
            '<html><body><a href="/a">a</a><a href="/b">b</a></body></html>',
            "text/html",
        ),
        "/a": (200, "<html>a</html>", "text/html"),
        "/b": (200, "<html>b</html>", "text/html"),
    }
    payload = {
        "url": "http://example.com/",
        # Catastrophic-but-valid pattern: bounded matching (N-1) runs it with
        # a wall-clock budget instead of rejecting it at compile. It genuinely
        # matches /a (ends in "a") and not /b, so scope filtering now WORKS
        # where the old reject-on-shape guard silently dropped the filter.
        "policy": {"limits": {"max_depth": 1, "max_pages": 10}, "scope_regex": CATASTROPHIC},
    }
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    urls = {r["normalized_url"] for r in out["results"]}
    assert "http://example.com/a" in urls
    assert "http://example.com/b" not in urls


async def test_recursive_scope_regex_timeout_fails_closed(monkeypatch):
    # A pathological pattern that defeats the regex engine's optimizer must
    # raise RegexTimeout, which the scope filter treats as out-of-scope
    # (fail-closed): the crawl completes, nothing hangs.
    from app.safe_regex import RegexTimeout

    calls = {"n": 0}

    def _always_timeout(pattern, *args, **kwargs):
        calls["n"] += 1
        raise RegexTimeout("test-induced budget expiry")

    # The bounded scope check is deduped into app.crawl.scope_match (M10), so
    # match_bounded is invoked there; patching that one seam now guards both
    # the recursive loop and the sitemap-seed filter.
    monkeypatch.setattr("app.crawl.scope_match.match_bounded", _always_timeout)
    pages = {
        "/": (200, '<html><body><a href="/a">a</a><a href="/b">b</a></body></html>', "text/html"),
        "/a": (200, "<html>a</html>", "text/html"),
        "/b": (200, "<html>b</html>", "text/html"),
    }
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": 1, "max_pages": 10}, "scope_regex": r"^/keep/"},
    }
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    assert calls["n"] > 0, "bounded match never invoked"
    urls = {r["normalized_url"] for r in out["results"]}
    assert "http://example.com" in urls
    assert "http://example.com/a" not in urls
    assert "http://example.com/b" not in urls


async def test_recursive_scope_regex_still_filters_with_valid_pattern(monkeypatch):
    # Valid scope patterns must still filter links, now via the compiled pattern.
    urls = await run_scoped_crawl(r"/keep/")
    assert "http://example.com/keep/x" in urls
    assert "http://example.com/drop/y" not in urls
