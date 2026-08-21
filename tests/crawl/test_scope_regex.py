"""Adversarial fix (MEDIUM): ReDoS guard for user-controlled scope_regex.

scope_regex is applied to every discovered link URL. A pattern such as
``(a+)+$`` triggers catastrophic backtracking and can hang the request. We
refuse to compile pathological patterns and bound the number of links scanned.
"""
import pytest

from app.crawl.engine import _compile_scope_regex_safe, _filter_links_by_scope
from app.models import EngineCrawlResult


def _make_result(urls):
    links = [type("_L", (), {"normalized_url": u})() for u in urls]
    return EngineCrawlResult(
        original_url="http://x.example/",
        normalized_url="http://x.example/",
        final_url="http://x.example/",
        status="success",
        classification="rendered",
        selected_strategy=None,
        attempted_strategies=[],
        http_status=200,
        headers={},
        content_type="text/html",
        html="",
        text="",
        extraction_method=None,
        quality_score=1.0,
        discovered_links=links,
        discovered_link_count=len(links),
        discovered_links_truncated=False,
    )


def test_safe_compile_rejects_nested_quantifiers():
    for evil in ["(a+)+$", "(a|a)+$", "(a*)*", "(?:a+)+b", "(a+)+c"]:
        assert _compile_scope_regex_safe(evil) is None, evil


def test_safe_compile_rejects_overlong_pattern():
    assert _compile_scope_regex_safe("a" * 300) is None


def test_safe_compile_accepts_normal_pattern():
    pat = _compile_scope_regex_safe(r"example\.com")
    assert pat is not None
    assert pat.search("http://example.com/x")


def test_filter_keeps_matching_links():
    res = _make_result(["http://example.com/a", "http://other.com/b"])
    out = _filter_links_by_scope(res, r"example\.com")
    kept = {l.normalized_url for l in out.discovered_links}
    assert kept == {"http://example.com/a"}


def test_filter_rejects_catastrophic_pattern_without_hanging():
    # A pathological pattern must be ignored (result unchanged), not hang.
    res = _make_result(["http://x.example/" + "a" * 50])
    out = _filter_links_by_scope(res, "(a+)+$")
    assert out.discovered_link_count == 1
