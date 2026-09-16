"""Sync post-fetch discovery parsing must leave the event loop.

``_parse_html_tree`` / ``_html_diagnostics`` / ``_extract_discovered_links`` /
``extract_js_endpoints`` / ``_detect_pagination`` run synchronously in both
strategy result builders. The post-fetch block must move to
``asyncio.to_thread``, returning plain data (no lxml tree escapes).
"""

import asyncio

from tests._testkit import (
    http_strategy_policy,
    install_to_thread_spy,
    invoke_browser_strategy,
    invoke_http_strategy,
)

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
