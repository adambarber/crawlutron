"""Pagination detection: rel=next on <a> and <link>, resolved, non-http dropped."""

from __future__ import annotations

from crawlutron_extract import detect_pagination


def test_link_rel_next_is_returned():
    out = detect_pagination('<link rel="next" href="/page2">', "http://example.com/")
    assert str(out) == "http://example.com/page2"


def test_anchor_rel_next_is_returned():
    out = detect_pagination('<a rel="next" href="http://example.com/page2">next</a>', "http://example.com/")
    assert str(out) == "http://example.com/page2"


def test_nofollow_next_still_matches():
    out = detect_pagination('<a rel="nofollow next" href="/page3">next</a>', "http://example.com/")
    assert str(out) == "http://example.com/page3"


def test_relative_href_resolves_against_final_url():
    out = detect_pagination('<link rel="next" href="page2">', "http://example.com/page1")
    assert str(out) == "http://example.com/page2"


def test_none_present_returns_none():
    assert detect_pagination('<a href="/page2">next</a>', "http://example.com/") is None
    assert detect_pagination("<html></html>", "http://example.com/") is None


def test_non_http_href_dropped():
    assert detect_pagination('<link rel="next" href="mailto:x@y">', "http://example.com/") is None
    assert detect_pagination('<link rel="next" href="javascript:go()">', "http://example.com/") is None


def test_empty_html_or_url_returns_none():
    assert detect_pagination("", "http://example.com/") is None
    assert detect_pagination('<link rel="next" href="/page2">', None) is None
