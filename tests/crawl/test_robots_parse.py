"""The shared robots.txt line-scanner + group selector (jscpd #5, #1766 family).

Three hand-rolled scanners used to parse one robots.txt body: crawl-delay
(module level) and rules + sitemaps (nested closures in engine.py). They had
already drifted on an empty-value gate. These tests pin the unified
contract, including the traps a naive unification falls into.
"""

from __future__ import annotations

import pytest

from app.crawl.robots_parse import (
    iter_robots_fields,
    parse_robots_crawl_delay,
    parse_robots_rules,
    parse_robots_sitemaps,
    select_robots_values,
)

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
