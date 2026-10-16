"""Sync post-fetch discovery parsing must leave the event loop.

The parse/extract/detect block runs synchronously in both strategy result
builders, so it must move to ``asyncio.to_thread`` and return plain data.
"""

import asyncio

from lxml import etree

from app.admission.policy import read_policy
from app.extraction.content import extract_post_fetch_discovery
from app.models.crawl_policy import CrawlPolicy
from app.models.normalized_url import NormalizedUrl
from app.safety.validation import HostPolicy
from tests._assertions import install_to_thread_spy
from tests._fetcher_factories import invoke_browser_strategy, invoke_http_strategy, make_fetch
from tests._policy_factories import canonical, http_strategy_policy

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

    Hand the strategy the production model, so a field the builder starts
    reading cannot be missing here without this failing.
    """
    return make_fetch(url=url, final_url=url, body=HTML.encode(), ctype="text/html")


#: What the post-fetch block finds in ``HTML``, stated once.
EXPECTED_LINKS = frozenset(
    canonical(url) for url in ("http://example.com/p1", "http://example.com/p2", "http://example.com/next")
)
EXPECTED_JS_ENDPOINTS = [{"url": "http://example.com/api/data"}]
NEXT_PAGE_URL = "http://example.com/next"

#: The visible text the browser worker reports for ``HTML``: enough words for the extractor to classify the
#: page, and enough of them to be an extraction rather than a title.
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

    The count sits next to the set because the set deduplicates: three URLs is
    not the same claim as exactly three links found.
    """
    assert result.discovered_link_count == len(EXPECTED_LINKS)
    assert {link.normalized_url for link in result.discovered_links} == EXPECTED_LINKS
    assert result.diagnostics["js_endpoints"] == EXPECTED_JS_ENDPOINTS
    assert result.next_page_url == canonical(NEXT_PAGE_URL)


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
    import app.extraction.content as ext_mod

    out = await asyncio.to_thread(
        ext_mod.extract_post_fetch_discovery,
        HTML,
        canonical("http://example.com/"),
        js_base_url=canonical("http://example.com/"),
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
    assert out["next_page_url"] == canonical("http://example.com/next")


def test_html_diagnostics_omit_policy_blocked_frame_and_private_form(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    html = '<iframe src="https://blocked.test/frame"></iframe><form action="http://127.0.0.1/admin"></form>'
    # The source carries the policy under test: its own ``resolve`` is the filter, so admitting it under an
    # unrestricted policy would defeat the very contract this test pins.
    policy = HostPolicy.from_strings("allowed.test", "blocked.test")
    source = NormalizedUrl("https://allowed.test/page", policy=policy)
    result = extract_post_fetch_discovery(
        html,
        source,
        include_diagnostics=True,
        host_policy=policy,
    )
    assert result["diagnostics"] == {}


async def test_discovery_feeds_are_policy_filtered():
    from app.transport.fetch_policy import discovery_diagnostics

    html = (
        '<link rel="alternate" type="application/rss+xml" href="https://blocked.test/feed">'
        '<link rel="alternate" type="application/atom+xml" href="http://127.0.0.1/feed">'
        '<link rel="alternate" type="application/rss+xml" href="https://allowed.test/feed">'
    )
    # The page value carries the job policy: its own ``resolve`` is what keeps the blocked-host and private
    # feeds out of the answer.
    result = await discovery_diagnostics(
        normalized_url=NormalizedUrl(
            "https://allowed.test/page", policy=HostPolicy.from_strings("allowed.test", "blocked.test")
        ),
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

    ``detect_pagination`` resolves an attacker ``href``; the HTTP builder
    filtered it, the browser builder assigned it raw -- ``javascript:`` escaped.
    """
    malicious = '<html><body><a rel="next" href="javascript:alert(1)">next</a></body></html>'
    result = await _render(monkeypatch, malicious, text="body copy")
    assert result.status == "success"
    assert result.next_page_url is None
    assert "next_page_url" not in result.diagnostics


async def test_browser_result_carries_the_shared_builder_shape(monkeypatch):
    """The browser path must build its result through build_engine_result.

    It hand-built EngineCrawlResult, silently relying on the ``validators``
    default; pinning the fields proves the shared-builder route preserves them.
    """
    result = await _render(monkeypatch, HTML)
    assert result.status == "success"
    assert result.classification == "rendered"
    assert result.selected_strategy == "BROWSER_HEADLESS"
    # No HTTP layer on the browser path: these stay empty/None.
    assert result.http_status is None
    assert result.headers == {}
    assert result.content_type is None
    # A render carries no origin validators (no etag/last-modified); the field must be an empty dict, never
    # None or absent.
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

    Every discovery field reaches a result through one chokepoint; one threaded
    into only one strategy shows up only as a difference between the pair.
    """
    http = await invoke_http_strategy(monkeypatch, policy=_full_discovery_policy(), resp=_page_resp)
    browser = await _render(monkeypatch, HTML)
    for field in ("discovered_link_count", "discovered_links_truncated", "next_page_url", "error"):
        assert getattr(http, field) == getattr(browser, field), field
    assert {link.normalized_url for link in http.discovered_links} == {
        link.normalized_url for link in browser.discovered_links
    }
