"""Sync post-fetch discovery parsing must leave the event loop.

``_parse_html_tree`` / ``html_diagnostics`` / ``extract_discovered_links`` /
``extract_js_endpoints`` / ``detect_pagination`` run synchronously in both
strategy result builders. The post-fetch block must move to
``asyncio.to_thread``, returning plain data (no lxml tree escapes).
"""

import asyncio

from lxml import etree

from app.api_models import CrawlPolicy
from app.crawl.extraction import extract_post_fetch_discovery
from app.crawl.policy import read_policy
from app.validation import HostPolicy
from tests._assertions import install_to_thread_spy
from tests._fetcher_factories import invoke_browser_strategy, invoke_http_strategy, make_fetch
from tests._policy_factories import http_strategy_policy

HTML = (
    "<html><head><title>T</title></head><body>"
    '<form action="/submit" method="post"><input name="q"></form>'
    '<iframe src="/frame"></iframe>'
    '<a href="/p1">page one with enough words to classify</a>'
    '<a href="/p2">page two with enough words to classify</a>'
    '<a href="/next" rel="next">next page here</a>'
    '<script>fetch("/api/data")</script>'
    "<p>Body copy to push the visible text well past any threshold.</p>"
    "</body></html>"
)


def _page_resp(url):
    """The 200 the HTTP strategy will read, as a real ``HttpFetchResult``.

    The strategy is handed the production model rather than a local object with
    the same handful of attributes, so a field the builder starts reading
    cannot be missing here without this failing.
    """
    return make_fetch(url=url, final_url=url, body=HTML.encode(), ctype="text/html")


#: What the post-fetch block finds in ``HTML``, stated once. Both strategy
#: builders run that block and hand back the same discoveries, so the
#: expectation belongs with the page rather than with either path through it.
EXPECTED_LINKS = frozenset({"http://example.com/p1", "http://example.com/p2", "http://example.com/next"})
EXPECTED_JS_ENDPOINTS = [{"url": "http://example.com/api/data"}]
NEXT_PAGE_URL = "http://example.com/next"

#: The visible text the browser worker reports for ``HTML``: enough words for
#: the extractor to classify the page, and enough of them to be an extraction
#: rather than a title.
BROWSER_TEXT = "page one page two next page body copy"


def _full_discovery_policy() -> CrawlPolicy:
    """Full extraction with discovery on: the policy that makes the post-fetch
    block have something to find, for whichever strategy is under test."""
    return http_strategy_policy(extraction="full", discovery=True)


async def _render(monkeypatch, html: str, *, text: str = BROWSER_TEXT):
    """The browser strategy's result for `html`, built by the production builder."""
    return await invoke_browser_strategy(monkeypatch, policy=_full_discovery_policy(), html=html, text=text)


def assert_extracted_discovery(result) -> None:
    """Assert the shared post-fetch discoveries, whichever builder produced them.

    The count is asserted next to the set because the set deduplicates: three
    distinct URLs is not the same claim as exactly three links found, and a
    duplicated discovery is exactly the kind of drift a flat row would hide.
    """
    assert result.discovered_link_count == len(EXPECTED_LINKS)
    assert {link.normalized_url for link in result.discovered_links} == EXPECTED_LINKS
    assert result.diagnostics["js_endpoints"] == EXPECTED_JS_ENDPOINTS
    assert result.next_page_url == NEXT_PAGE_URL


async def test_http_post_fetch_block_runs_off_loop(monkeypatch):
    seen = install_to_thread_spy(monkeypatch)
    result = await invoke_http_strategy(
        monkeypatch,
        policy=_full_discovery_policy(),
        resp=_page_resp,
    )
    assert result.status == "success"
    assert "extract_post_fetch_discovery" in seen
    assert_extracted_discovery(result)
    assert result.diagnostics["forms"][0]["normalized_action"] == "http://example.com/submit"
    assert result.diagnostics["frames"][0]["normalized_url"] == "http://example.com/frame"


async def test_browser_post_fetch_block_runs_off_loop(monkeypatch):
    seen = install_to_thread_spy(monkeypatch)
    result = await _render(monkeypatch, HTML)
    assert result.status == "success"
    assert "extract_post_fetch_discovery" in seen
    assert_extracted_discovery(result)
    assert "forms" in result.diagnostics and "frames" in result.diagnostics


async def test_no_lxml_tree_escapes_to_loop(monkeypatch):
    """The offloaded block returns plain data: no lxml element crosses back."""
    import app.crawl.extraction as ext_mod

    out = await asyncio.to_thread(
        ext_mod.extract_post_fetch_discovery,
        HTML,
        "http://example.com/",
        js_base_url="http://example.com/",
        max_links=10,
        include_diagnostics=True,
        include_links=True,
        include_js_endpoints=True,
        host_policy=HostPolicy.unrestricted(),
    )

    def _has_element(value):
        if isinstance(value, etree._Element):
            return True
        if isinstance(value, dict):
            return any(_has_element(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(_has_element(v) for v in value)
        return False

    assert not _has_element(out)
    assert out["discovered_link_count"] == 3
    assert out["next_page_url"] == "http://example.com/next"


def test_html_diagnostics_omit_policy_blocked_frame_and_private_form(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    html = '<iframe src="https://blocked.test/frame"></iframe><form action="http://127.0.0.1/admin"></form>'
    result = extract_post_fetch_discovery(
        html,
        "https://allowed.test/page",
        include_diagnostics=True,
        host_policy=HostPolicy.from_strings("allowed.test", "blocked.test"),
    )
    assert result["diagnostics"] == {}


async def test_discovery_feeds_are_policy_filtered():
    from app.crawl.strategies.fetch_policy import _discovery_diagnostics

    html = (
        '<link rel="alternate" type="application/rss+xml" href="https://blocked.test/feed">'
        '<link rel="alternate" type="application/atom+xml" href="http://127.0.0.1/feed">'
        '<link rel="alternate" type="application/rss+xml" href="https://allowed.test/feed">'
    )
    result = await _discovery_diagnostics(
        normalized_url="https://allowed.test/page",
        html=html,
        policy=read_policy({"discovery": {"feeds": True}}),
        host_policy=HostPolicy.from_strings("allowed.test", "blocked.test"),
        headers={},
        http_fetcher=lambda *args: None,
        engine_robots_sitemaps=[],
        robots_evaluated=True,
    )
    assert result["discovery"]["feeds"] == ["https://allowed.test/feed"]


async def test_browser_path_filters_malicious_next_page_url(monkeypatch):
    """The browser result must not surface an unfiltered attacker href.

    ``detect_pagination`` resolves an ``href`` out of attacker-controlled
    HTML. The HTTP builder filtered the resolved URL before use; the browser
    builder assigned it raw to both diagnostics and the result field, so a
    ``javascript:`` rel=next escaped into the response. Egress now happens at
    the producer, so every consumer is covered.
    """
    malicious = '<html><body><a rel="next" href="javascript:alert(1)">next</a></body></html>'
    result = await _render(monkeypatch, malicious, text="body copy")
    assert result.status == "success"
    assert result.next_page_url is None
    assert "next_page_url" not in result.diagnostics


async def test_browser_result_carries_the_shared_builder_shape(monkeypatch):
    """The browser path must build its result through _make_engine_result.

    The HTTP path routes through the shared builder; the browser path
    hand-built EngineCrawlResult, silently relying on the ``validators``
    default (a render has no etag). This pins every field the hand-build
    produced for this page so routing through the shared builder is provably
    behavior-preserving -- and a field that silently changes is caught here.
    """
    result = await _render(monkeypatch, HTML)
    assert result.status == "success"
    assert result.classification == "rendered"
    assert result.selected_strategy == "BROWSER_HEADLESS"
    # No HTTP layer on the browser path: these stay empty/None.
    assert result.http_status is None
    assert result.headers == {}
    assert result.content_type is None
    # A render carries no origin validators (no etag/last-modified); the field
    # must be an empty dict, never None or absent.
    assert result.validators == {}
    assert result.error is None
    assert result.html == HTML
    assert isinstance(result.text, str)
    assert "page one with enough words" in result.text
    assert isinstance(result.markdown, str)
    assert isinstance(result.extraction_method, str)
    assert result.classification_evidence == []
    assert_extracted_discovery(result)
    assert result.diagnostics["next_page_url"] == NEXT_PAGE_URL
    # One attempt, matching the HTTP path's single-attempt shape.
    (attempt,) = result.attempted_strategies
    assert attempt.strategy == "BROWSER_HEADLESS"
    assert attempt.status == "success"
    assert attempt.http_status is None


async def test_both_strategies_agree_on_the_discovery_result_fields(monkeypatch):
    """The two paths over one page must not disagree about a discovery field.

    Every discovered-links field reaches a result through
    ``content_pipeline._discovery_result_fields``, and this is the behavioural
    half of that chokepoint: a field threaded into one strategy and not the
    other is invisible in both results -- each is individually well-formed --
    and shows up only as a difference between them. Stated on the two real
    builders, over one page, so the pair is what is compared.
    """
    http = await invoke_http_strategy(monkeypatch, policy=_full_discovery_policy(), resp=_page_resp)
    browser = await _render(monkeypatch, HTML)
    for field in ("discovered_link_count", "discovered_links_truncated", "next_page_url", "error"):
        assert getattr(http, field) == getattr(browser, field), field
    assert {link.normalized_url for link in http.discovered_links} == {
        link.normalized_url for link in browser.discovered_links
    }
