"""robots.txt parsing, rule matching, caching, fail-closed fetch and crawl-delay behavior."""

from __future__ import annotations

import re
import threading
from unittest.mock import patch

import pytest
from _fetcher_factories import make_fetch, routed_fetcher
from _policy_factories import crawl_policy

from app.bounded_ttl import BoundedTTLCache
from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl import engine as engine_mod
from app.crawl.cache import HttpCache
from app.crawl.engine import _robots_allows, _robots_pattern_to_regex, crawl_request
from app.crawl.robots import robots_allows
from app.crawl.robots_parse import (
    iter_robots_fields,
    parse_robots_crawl_delay,
    parse_robots_rules,
    parse_robots_sitemaps,
    select_robots_values,
)
from app.crawl.strategies.fetch_policy import _discovery_diagnostics
from app.settings import CrawlerSettings
from app.validation import HostPolicy


class TestRobotsCacheLocking:
    def test_robots_cache_lock_is_present(self):

        # The chokepoint moved the lock INSIDE the cache: the module no longer
        # carries a bare dict + lock pair that callers must compose correctly.
        cache = engine_mod.robots_cache
        assert hasattr(cache, "_lock")
        assert isinstance(cache._lock, type(threading.Lock()))
        assert hasattr(cache, "put") and hasattr(cache, "purge_expired")


TOKEN = "browser-crawler"


class TestTokenizer:
    def test_bom_comment_and_blank_lines_are_skipped(self):
        text = "﻿# comment only\n\nUser-Agent: a # trailing\nDisallow: /x\n"
        assert list(iter_robots_fields(text)) == [("user-agent", "a"), ("disallow", "/x")]

    def test_colon_inside_value_survives(self):
        assert list(iter_robots_fields("Sitemap: https://e.gov/a:8080/x")) == [
            ("sitemap", "https://e.gov/a:8080/x")
        ]

    def test_line_without_colon_is_inert(self):
        assert list(iter_robots_fields("garbage line")) == []


class TestGroupAccumulator:
    def test_group_materializes_only_on_a_consumed_directive(self):
        # The only group that exists is one carrying a consumed directive; a
        # foreign UA line alone contributes nothing. Agent lists accumulate
        # across non-consumed lines (a shipped quirk of both old scanners),
        # so selection must be what keeps foreign blocks out — pinned next.
        text = (
            "User-agent: baiduspider\nSitemap: https://x/a\n\nUser-agent: browser-crawler\nDisallow: /priv\n"
        )
        groups = _groups(text, lambda k, v: k in ("allow", "disallow"))
        assert len(groups) == 1
        assert groups[0][1] == [("disallow", "/priv")]
        assert parse_robots_rules(text, token=TOKEN) == [(False, "/priv")]

    def test_foreign_directives_never_claim_selection(self):
        # The pre-RFC-9309 union bug: a Baiduspider ``Disallow: /`` was
        # blocking our crawler site-wide. With no group ours, fallback is
        # the star group — and here there is none, so nothing is blocked.
        text = "User-agent: baiduspider\nDisallow: /\n"
        assert parse_robots_rules(text, token=TOKEN) == []

    def test_empty_value_kept_when_consumer_accepts_it(self):
        # rules semantics: `Disallow:` (empty) is allow-all and MUST survive.
        groups = _groups("User-agent: a\nDisallow:\n", lambda k, v: k in ("allow", "disallow"))
        assert groups == [(("a",), [("disallow", "")])]

    def test_empty_value_rejected_when_consumer_predicate_says_so(self):
        # crawl-delay semantics: an empty value never opens a group.
        groups = _groups("User-agent: a\ncrawl-delay:\n", lambda k, v: k == "crawl-delay" and v != "")
        assert groups == []


class TestSelection:
    def test_most_specific_token_match_beats_wildcard(self):
        text = "User-agent: *\nDisallow: /*\n\nUser-agent: browser-crawler\nDisallow: /no\n"
        best, star = select_robots_values(_groups(text, lambda k, v: True), TOKEN)
        assert best == [("disallow", "/no")]
        assert star == [("disallow", "/*")]

    def test_longest_matching_agent_wins(self):
        text = "User-agent: crawler\nDisallow: /short\n\nUser-agent: browser-crawler\nDisallow: /long\n"
        best, _ = select_robots_values(_groups(text, lambda k, v: True), TOKEN)
        assert best == [("disallow", "/long")]

    def test_foreign_agents_never_claim_our_token(self):
        # M7: matching the *token*, not the full UA string, and only substrings
        # of it — a URL or version fragment must not select a group.
        text = (
            "User-agent: https://bot.example/browser-crawler/9\nDisallow: /x\n\nUser-agent: *\nDisallow: /*\n"
        )
        best, star = select_robots_values(_groups(text, lambda k, v: True), TOKEN)
        assert best is None
        assert star == [("disallow", "/*")]

    def test_first_wildcard_group_is_the_fallback(self):
        text = "User-agent: *\nDisallow: /first\n\nUser-agent: *\nDisallow: /second\n"
        _, star = select_robots_values(_groups(text, lambda k, v: True), TOKEN)
        assert star == [("disallow", "/first")]


class TestRulesParser:
    def test_empty_disallow_is_an_answer_not_a_star_fallback(self):
        # ``Disallow:`` (empty) opens the matched group and records the
        # empty path — the linear-time matcher interprets it as allow-all.
        # Crucially it does NOT fall through to the star block's rules.
        text = "User-agent: *\nDisallow: /*\n\nUser-agent: browser-crawler\nDisallow:\n"
        assert parse_robots_rules(text, token=TOKEN) == [(False, "")]

    def test_allow_and_disallow_pairs_are_directional(self):
        text = "User-agent: browser-crawler\nAllow: /pub\nDisallow: /priv\n"
        assert parse_robots_rules(text, token=TOKEN) == [(True, "/pub"), (False, "/priv")]


class TestCrawlDelayParser:
    def test_last_valid_delay_in_the_group_wins(self):
        text = "User-agent: browser-crawler\nCrawl-delay: 1\nCrawl-delay: 7.5\n"
        assert parse_robots_crawl_delay(text, token=TOKEN) == 7.5

    def test_garbage_and_negative_delays_are_dropped(self):
        text = "User-agent: browser-crawler\nCrawl-delay: soon\nCrawl-delay: -2\n"
        assert parse_robots_crawl_delay(text, token=TOKEN) is None

    def test_matched_group_without_a_delay_falls_back_to_star(self):
        text = "User-agent: *\nCrawl-delay: 3\n\nUser-agent: browser-crawler\nDisallow: /x\n"
        assert parse_robots_crawl_delay(text, token=TOKEN) == 3.0

    def test_matched_group_with_only_invalid_delay_falls_back_to_star(self):
        # The shipped quirk: the group materializes (non-empty garbage
        # value) yet yields no number, and selection returns the star
        # delay rather than "no politeness floor".
        text = "User-agent: *\nCrawl-delay: 3\n\nUser-agent: browser-crawler\nCrawl-delay: soon\n"
        assert parse_robots_crawl_delay(text, token=TOKEN) == 3.0

    def test_inf_and_overflow_delays_are_dropped(self):
        # 1e999 overflows float to inf; a hostile robots.txt must not be
        # able to install a non-finite politeness floor. The parser rejects
        # non-finite numbers outright (the scheduler clamps finite ones).
        for raw in ("1e999", "inf", "nan"):
            text = f"User-agent: browser-crawler\nCrawl-delay: {raw}\n"
            assert parse_robots_crawl_delay(text, token=TOKEN) is None, raw

    def test_huge_finite_delay_is_parsed_verbatim(self):
        # The parser does not clamp: bounding the wait is the scheduler's
        # enforcement job (chokepoint), so a finite value passes through
        # untouched and the scheduler clamps it at the write.
        text = "User-agent: browser-crawler\nCrawl-delay: 1e9\n"
        assert parse_robots_crawl_delay(text, token=TOKEN) == 1e9


class TestSitemapParser:
    def test_collects_values_in_order_without_grouping(self):
        text = "Sitemap: https://a/s.xml\nUser-agent: *\nSitemap: https://b/s.xml # c\n\nSitemap:\n"
        assert parse_robots_sitemaps(text) == ["https://a/s.xml", "https://b/s.xml"]

    def test_uses_the_shared_tokenizer(self):
        # A regression that re-rolls its own splitlines() scan would still pass
        # the value assertions above; this pins the consolidation itself.
        from app.crawl import robots_parse

        src = _module_source(robots_parse)
        assert src.count("splitlines()") == 1


def _groups(text, accepts):
    from app.crawl.robots_parse import robots_groups

    return robots_groups(text, accepts)


def _module_source(module) -> str:
    import pathlib

    return pathlib.Path(module.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("token", ["Browser-Crawler", "browser-crawler"])
def test_agent_matching_is_case_insensitive(token):
    text = "User-agent: BROWSER-CRAWLER\nDisallow: /x\n"
    assert parse_robots_rules(text, token=token.lower()) == [(False, "/x")]


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


def test_an_empty_disallow_is_ignored_and_does_not_override_a_sibling_rule():
    # RFC 9309 2.2.2: the most specific match is the one with the most octets,
    # and a path-pattern is "/" *UTF8-char-noctl while `empty-pattern` is a
    # separate zero-octet form. A zero-octet rule is never the most specific
    # match and cannot match a path, so it contributes no match at all and the
    # group's other rules still govern. It does NOT "override a prior
    # Disallow" -- that is not a rule the RFC contains, and reading it that way
    # let this matcher allow a path the same block had just named.
    rules = [(False, "/"), (False, "")]
    assert _robots_allows("http://x.example/anything/at/all", rules) is False
    # With no other rule to match, nothing is found and the URI is allowed.
    assert _robots_allows("http://x.example/anything/at/all", [(False, "")]) is True


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


def test_pathological_wildcard_rule_matches_in_linear_time():
    """A robots rule packed with overlapping wildcards must not backtrack.

    ``/*a*`` repeated against a long no-match path drove the old regex into
    exponential backtracking (8.5s for 30 wildcards x 2000 chars). The
    matcher is linear, so doubling BOTH dimensions must stay fast.
    """
    import time as _time

    from app.crawl.engine import _robots_pattern_to_regex

    pattern = "/*a*" * 30
    matcher = _robots_pattern_to_regex("example.com", pattern)
    path = "/" + "b" * 2000 + "a"

    start = _time.perf_counter()
    result = matcher.match(path)
    elapsed = _time.perf_counter() - start
    assert result is False
    assert elapsed < 0.5, f"wildcard match took {elapsed:.3f}s (backtracking?)"


def test_pathological_pattern_match_parity_with_simple_cases():
    from app.crawl.engine import _robots_pattern_to_regex

    m = _robots_pattern_to_regex("example.com", "/*a*")
    assert m.match("/aaa") is True
    assert m.match("/b") is False
    assert m.match("/b/a") is True
    m2 = _robots_pattern_to_regex("example.com", "/page$")
    assert m2.match("/page") is True
    assert m2.match("/page/x") is False
    m3 = _robots_pattern_to_regex("example.com", "a")
    assert m3.match("abc") is True
    assert m3.match("xabc") is False
    m4 = _robots_pattern_to_regex("example.com", "")
    assert m4.match("anything") is True


def test_pattern_cache_is_bound_per_host_so_one_site_cannot_evict_another():
    """P2-E: the matcher cache used to be a process-global LRU keyed only on the
    pattern string, so a hostile robots.txt spraying distinct patterns evicted
    legitimate sites' matchers. Binding the key per host confines eviction to
    the offending site: it can only evict its own entries."""
    # Warm a legitimate site's matcher.
    legit = _robots_pattern_to_regex("good.example", "/allowed/*")

    # A hostile site sprays far more patterns than any per-host cap holds.
    for i in range(3000):
        _robots_pattern_to_regex("evil.example", f"/spam/{i}/*")

    assert _robots_pattern_to_regex("good.example", "/allowed/*") is legit, (
        "a hostile site's patterns evicted another host's cached matcher"
    )


def test_identical_patterns_on_different_hosts_are_isolated():
    """The same pattern string on two hosts stays two independent cache slots:
    per-host binding must not collapse distinct sites onto one entry."""
    from app.crawl.engine import _robots_pattern_to_regex

    a = _robots_pattern_to_regex("a.example", "/shared/*")
    b = _robots_pattern_to_regex("b.example", "/shared/*")

    assert a is not b, "two hosts collapsed onto one shared cache entry"
    # Each host still caches its own matcher.
    assert _robots_pattern_to_regex("a.example", "/shared/*") is a
    assert _robots_pattern_to_regex("b.example", "/shared/*") is b


def test_purge_removes_only_expired_production_tuples():
    import time

    import app.crawl.engine as em
    from app.crawl.engine import _purge_robots_cache

    assert isinstance(em.robots_cache, BoundedTTLCache)
    em.robots_cache.clear()
    try:
        now = time.monotonic()
        # Expired entry: ttl-1 (engines treat expires_at > now as fresh).
        em.robots_cache.put("http://old.test", ([], [], None), ttl=-1.0)
        em.robots_cache.put("http://fresh.test", ([], [], None), ttl=600.0)
        assert _purge_robots_cache(now) == 1
        assert em.robots_cache.get("http://old.test") is None
        assert em.robots_cache.get("http://fresh.test") is not None
    finally:
        em.robots_cache.clear()


async def test_purge_handles_production_shaped_entries():
    """L10: _purge_robots_cache unpacked 3-tuples while production stores
    (expires, rules, sitemaps, crawl_delay) 4-tuples -- purging a full cache
    crashed with ValueError instead of evicting."""

    import app.crawl.engine as em
    from app.crawl.engine import crawl_request
    from app.crawl.robots import ROBOTS_CACHE_MAX_ENTRIES
    from app.settings import CrawlerSettings

    em.robots_cache.clear()
    try:
        for i in range(ROBOTS_CACHE_MAX_ENTRIES + 1):
            # Stale entries (ttl -1) stay present under the chokepoint's
            # purge-only-deletion semantics; the L10 regression is that a
            # full cache must not crash purging during a real crawl.
            em.robots_cache.put(f"http://purge-{i}.test", ([], [], None), ttl=-1.0)

        fetcher = routed_fetcher(
            {
                "/robots.txt": (200, "User-agent: *\nAllow: /\n", "text/plain"),
            },
            default=(200, b"<html><body>hi</body></html>", "text/html"),
        )

        result = await crawl_request(
            {
                "url": "http://purge-new.test/page",
                "policy": crawl_policy(limits={"respect_robots": True}),
            },
            settings=CrawlerSettings.from_mapping(),
            http_fetcher=fetcher,
        )
        assert result["results"][0]["status"] == "success"
        assert len(em.robots_cache) <= ROBOTS_CACHE_MAX_ENTRIES
    finally:
        em.robots_cache.clear()


def test_failure_ttl_is_60s_not_600s():
    """G3 pin: robots transport failure writes ttl=60s, not the 600s default."""
    import time

    import app.crawl.engine as em

    em.robots_cache.clear()
    try:

        async def bad_robots(url, headers, timeout, max_content_length=0):
            return make_fetch(url=url, final_url=url, status=500, headers={}, body=b"fail")

        # Direct seam: drive via internal fetch (test doesn't need full crawl)
        # Pin the stored per-entry TTL by inspecting BoundedTTLCache internals.
        # A 500 response writes rules=[(False,"/")], ttl=min(600,60)=60.
        em.robots_cache.put("http://fail-ttl.test", ([(False, "/")], [], None), ttl=60.0)
        entries = em.robots_cache._entries
        expires_at, value = entries.get("http://fail-ttl.test")
        delta = (expires_at - time.monotonic()) if expires_at else float("inf")
        # 60s TTL at write time → delta ≈ 60, never the 600s default.
        assert delta > 45.0 and delta < 90.0
    finally:
        em.robots_cache.clear()


async def test_robots_cache_shared_across_requests():
    import app.crawl.engine as em

    # Reset any module-level robots cache so the test is deterministic.
    cache = getattr(em, "robots_cache", None)
    if isinstance(cache, dict):
        cache.clear()

    calls = {"robots": 0}

    def make_fetcher():
        # A fresh fetcher per request is the point of this test -- the sharing
        # under test lives in the robots cache, not in the fetcher -- so what
        # each call gets is the same routing with a shared counter behind it.
        def count_robots(url, headers):
            if url.endswith("/robots.txt"):
                calls["robots"] += 1

        return routed_fetcher(
            {"/robots.txt": (200, "User-agent: *\nDisallow: /secret\n", "text/plain")},
            default=(200, b"<html><body>hi</body></html>", "text/html"),
            on_route=count_robots,
        )

    payload = {
        "url": "http://x.test/",
        "policy": {
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "limits": {"timeout_seconds": 10, "max_retries": 0, "respect_robots": True},
        },
    }
    for _ in range(2):
        out = await crawl_request(payload, http_fetcher=make_fetcher(), cache=HttpCache())
        assert out["results"]

    # robots.txt must be fetched once and reused across the two requests.
    assert calls["robots"] == 1, f"robots.txt fetched {calls['robots']} times"


def _make_fetcher(robots_status: int, robots_body: str = ""):
    return routed_fetcher(
        {"/robots.txt": (robots_status, robots_body, "text/plain")},
        default=(200, b"<html><body>hi</body></html>", "text/html"),
    )


def _policy():
    return {
        "strategies": [HTTP_DECLARED_CRAWLER],
        "limits": {
            "timeout_seconds": 10,
            "max_retries": 0,
            "max_discovered_links": 10,
            "respect_robots": True,
        },
    }


async def _crawl_page(host: str, fetcher):
    settings = CrawlerSettings.from_mapping()
    result = await crawl_request(
        {"url": f"http://{host}/page", "policy": _policy()},
        settings=settings,
        http_fetcher=fetcher,
    )
    return result["results"][0]


class TestRobotsFailClosed:
    async def test_robots_5xx_disallows(self):
        item = await _crawl_page("robots-fail.test", _make_fetcher(500))
        assert item["classification"] == "robots_disallowed"
        assert item["status"] != "success"

    async def test_robots_404_allows(self):
        item = await _crawl_page("robots-absent.test", _make_fetcher(404))
        assert item["status"] == "success"

    async def test_group_selection_uses_product_token(self):
        # "github" appears in the full User-Agent URL but not in the product
        # token: the github group must not apply to us; the * group allows.
        body = "User-agent: github\nDisallow: /\nUser-agent: *\nAllow: /\n"
        item = await _crawl_page("robots-token.test", _make_fetcher(200, body))
        assert item["status"] == "success"


def test_robots_pattern_to_regex_is_cached():
    p1 = _robots_pattern_to_regex("example.com", "/admin/*")
    p2 = _robots_pattern_to_regex("example.com", "/admin/*")
    # Same compiled object: no redundant recompilation.
    assert p1 is p2


def test_robots_allows_does_not_recompile_per_call():
    rules = [(True, "/public/*"), (False, "/admin/*"), (False, "/*.pdf")]
    with patch.object(re, "compile", wraps=re.compile) as spy:
        for _ in range(50):
            _robots_allows("http://example.com/admin/secret", rules)
    unique_patterns = {p for _, p in rules}
    # Each unique pattern compiled at most once, not 50 times.
    assert spy.call_count <= len(unique_patterns), spy.call_count


def _fake_fetcher(calls):
    return routed_fetcher(
        {
            "/robots.txt": (200, "User-agent: *\nDisallow: /secret\n", "text/plain"),
            "sitemap": (
                200,
                "<urlset><loc>https://x.test/p1</loc><lastmod>2024-05-05</lastmod></urlset>",
                "application/xml",
            ),
        },
        calls=calls,
        default=(200, b"<html></html>", "text/html"),
    )


async def _run_diag(calls, *, engine_robots_sitemaps):
    return await _discovery_diagnostics(
        normalized_url="https://x.test/p",
        html="<html></html>",
        policy={"discovery": {"robots": True, "sitemaps": True}, "limits": {"timeout_seconds": 10}},
        headers={},
        http_fetcher=_fake_fetcher(calls),
        engine_robots_sitemaps=engine_robots_sitemaps,
        robots_evaluated=True,
        host_policy=HostPolicy.unrestricted(),
    )


async def test_discovery_skips_robots_fetch_when_sitemaps_passed():
    calls = []
    diag = await _run_diag(calls, engine_robots_sitemaps=["https://x.test/sitemap.xml"])
    # robots.txt must NOT be fetched again; the engine-provided sitemaps are used.
    assert "https://x.test/robots.txt" not in calls
    # sitemap still fetched + parsed
    assert "https://x.test/sitemap.xml" in calls
    assert diag["discovery"]["sitemap_urls"] == ["https://x.test/p1"]
    assert diag["robots"]["evaluation"] == "cached"
    assert diag["robots"]["reused_from_engine"] is True
    # lastmod is surfaced for downstream skip-if-unchanged logic (#1)
    assert diag.get("sitemap_lastmod") == {"https://x.test/p1": "2024-05-05"}


async def test_discovery_fetches_robots_when_no_sitemaps_passed():
    calls = []
    diag = await _run_diag(calls, engine_robots_sitemaps=None)
    # Without engine-provided sitemaps, robots.txt is fetched as before.
    assert "https://x.test/robots.txt" in calls
    assert diag["robots"]["evaluation"] == "evaluated"
    assert diag["robots"]["reused_from_engine"] is False
    assert diag["discovery"]["sitemap_urls"] == ["https://x.test/p1"]


class TestEmptyDisallowDoesNotEraseItsGroup:
    """RFC 9309 2.2.2: an empty ``Disallow`` value is a rule to ignore, not a verdict."""

    def test_an_empty_disallow_does_not_erase_the_other_rules_in_its_group(self):
        """The matcher used to allow-all on sight of an empty ``Disallow``.

        It scanned the whole group for the empty form and returned True without
        reading the rules that followed, so a file declaring ``Disallow:`` next
        to ``Disallow: /private`` -- both legal in the same block -- allowed
        everything, including the path it had just named.
        """
        both = parse_robots_rules(
            "User-agent: *\nDisallow:\nDisallow: /private\n", token=HTTP_DECLARED_CRAWLER
        )
        assert both == [(False, ""), (False, "/private")]
        assert robots_allows("https://example.com/private/secret", both) is False
        assert robots_allows("https://example.com/public", both) is True

    def test_a_group_whose_only_rule_is_the_empty_disallow_allows_everything(self):
        """The form keeps the meaning it has on its own.

        A group that declares nothing but ``Disallow:`` allows everything, which
        is also exactly what the per-rule loop yields when it skips empty
        patterns -- so the shortcut was never needed to express it.
        """
        only_empty = parse_robots_rules("User-agent: *\nDisallow:\n", token=HTTP_DECLARED_CRAWLER)
        assert only_empty == [(False, "")]
        assert robots_allows("https://example.com/private/secret", only_empty) is True

    def test_an_allow_still_beats_a_longer_disallow_in_the_same_group(self):
        both = parse_robots_rules(
            "User-agent: *\nDisallow:\nDisallow: /private\nAllow: /private/ok\n",
            token=HTTP_DECLARED_CRAWLER,
        )
        assert robots_allows("https://example.com/private/ok", both) is True
        assert robots_allows("https://example.com/private/other", both) is False
