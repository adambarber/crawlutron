"""Sync post-fetch discovery parsing must leave the event loop.

``_parse_html_tree`` / ``_html_diagnostics`` / ``_extract_discovered_links`` /
``extract_js_endpoints`` / ``_detect_pagination`` run synchronously in both
strategy result builders. The post-fetch block must move to
``asyncio.to_thread``, returning plain data (no lxml tree escapes).
"""

import asyncio

from _assertions import install_to_thread_spy
from _fetcher_factories import invoke_browser_strategy, invoke_http_strategy
from _policy_factories import http_strategy_policy

from app.crawl.extraction import extract_post_fetch_discovery
from app.validation import HostPolicy

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
    class Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        body = HTML.encode()
        final_url = url
        elapsed_ms = 2

    return Resp()


async def test_http_post_fetch_block_runs_off_loop(monkeypatch):
    seen = install_to_thread_spy(monkeypatch)
    result = await invoke_http_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="full", discovery=True),
        resp=_page_resp,
    )
    assert result.status == "success"
    assert "extract_post_fetch_discovery" in seen
    assert result.discovered_link_count == 3
    assert {link.normalized_url for link in result.discovered_links} == {
        "http://example.com/p1",
        "http://example.com/p2",
        "http://example.com/next",
    }
    assert result.diagnostics["forms"][0]["normalized_action"] == "http://example.com/submit"
    assert result.diagnostics["frames"][0]["normalized_url"] == "http://example.com/frame"
    assert result.diagnostics["js_endpoints"] == [{"url": "http://example.com/api/data"}]
    assert result.next_page_url == "http://example.com/next"


async def test_browser_post_fetch_block_runs_off_loop(monkeypatch):
    seen = install_to_thread_spy(monkeypatch)
    result = await invoke_browser_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="full", discovery=True),
        html=HTML,
        text="page one page two next page body copy",
    )
    assert result.status == "success"
    assert "extract_post_fetch_discovery" in seen
    assert result.discovered_link_count == 3
    assert "forms" in result.diagnostics and "frames" in result.diagnostics
    assert result.diagnostics["js_endpoints"] == [{"url": "http://example.com/api/data"}]
    assert result.next_page_url == "http://example.com/next"


async def test_no_lxml_tree_escapes_to_loop(monkeypatch):
    """The offloaded block returns plain data: no lxml element crosses back."""
    from lxml import etree

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
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    html = (
        '<iframe src="https://blocked.test/frame"></iframe>'
        '<form action="http://127.0.0.1/admin"></form>'
    )
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
        policy={"discovery": {"feeds": True}},
        host_policy=HostPolicy.from_strings("allowed.test", "blocked.test"),
        headers={},
        http_fetcher=lambda *args: None,
        engine_robots_sitemaps=[],
        robots_evaluated=True,
    )
    assert result["discovery"]["feeds"] == ["https://allowed.test/feed"]


async def test_browser_path_filters_malicious_next_page_url(monkeypatch):
    """The browser result must not surface an unfiltered attacker href.

    ``_detect_pagination`` resolves an ``href`` out of attacker-controlled
    HTML. The HTTP builder filtered the resolved URL before use; the browser
    builder assigned it raw to both diagnostics and the result field, so a
    ``javascript:`` rel=next escaped into the response. Egress now happens at
    the producer, so every consumer is covered.
    """
    malicious = '<html><body><a rel="next" href="javascript:alert(1)">next</a></body></html>'
    result = await invoke_browser_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="full", discovery=True),
        html=malicious,
        text="body copy",
    )
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
    result = await invoke_browser_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="full", discovery=True),
        html=HTML,
        text="page one page two next page body copy",
    )
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
    assert result.discovered_link_count == 3
    assert {link.normalized_url for link in result.discovered_links} == {
        "http://example.com/p1",
        "http://example.com/p2",
        "http://example.com/next",
    }
    assert result.next_page_url == "http://example.com/next"
    assert result.diagnostics["js_endpoints"] == [{"url": "http://example.com/api/data"}]
    assert result.diagnostics["next_page_url"] == "http://example.com/next"
    # One attempt, matching the HTTP path's single-attempt shape.
    (attempt,) = result.attempted_strategies
    assert attempt.strategy == "BROWSER_HEADLESS"
    assert attempt.status == "success"
    assert attempt.http_status is None
