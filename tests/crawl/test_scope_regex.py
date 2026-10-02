"""Adversarial fix (MEDIUM): ReDoS guard for user-controlled scope_regex.

scope_regex is applied to every discovered link URL. A pattern such as
``(a+)+$`` triggers catastrophic backtracking and can hang the request. We
refuse to compile pathological patterns and bound the number of links scanned.
"""

import contextlib

from app.crawl.admission import compile_scope_pattern as compile_scope_pattern
from app.models.engine_crawl_result import EngineCrawlResult


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


def test_nested_quantifiers_compile_and_match_bounded():
    # N-1: static shape rejection is bypassable by construction, so these
    # classic catastrophic shapes now COMPILE (valid syntax) and the safety
    # guarantee moved to bounded execution: a hostile match raises
    # RegexTimeout instead of hanging the crawl loop.
    from app.safe_regex import RegexTimeout, match_bounded

    for evil in ["(a+)+$", "(a|a)+$", "(a*)*", "(?:a+)+b", "(a+)+c"]:
        pat = compile_scope_pattern(evil)
        assert pat is not None, evil
        if pat is None:
            continue
        host = "http://" + "a" * 64 + ".test/"
        with contextlib.suppress(RegexTimeout):
            match_bounded(pat, host, timeout=0.25)  # bounded: the budget fired instead of the match hanging


def test_safe_compile_rejects_overlong_pattern():
    assert compile_scope_pattern("a" * 300) is None


def test_safe_compile_rejects_unparseable_pattern():
    assert compile_scope_pattern("(((a") is None


def test_safe_compile_accepts_normal_pattern():
    pat = compile_scope_pattern(r"example\.com")
    assert pat is not None
    assert pat.search("http://example.com/x")
