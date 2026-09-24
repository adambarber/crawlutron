"""Sync trafilatura extraction must leave the event loop.

``_http_strategy_result`` (strategies/http.py) and ``_browser_strategy_result``
(engine.py) call ``_extract_html`` (trafilatura, CPU-bound) synchronously
inside async functions. Both must go through ``asyncio.to_thread`` like the
neighboring ``_extract_document``/``extract_content`` calls already do.
"""

from _assertions import install_to_thread_spy
from _fetcher_factories import invoke_browser_strategy, invoke_http_strategy
from _policy_factories import http_strategy_policy

from app.crawl.extraction import _extract_html

_HTML_BODY = (
    b"<html><head><title>T</title></head><body><article><p>"
    + b"Hello world, this is enough text to render. " * 20
    + b"</p></article></body></html>"
)


def _resp(url):
    class Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        body = _HTML_BODY
        final_url = url
        elapsed_ms = 3

    return Resp()


async def test_http_trafilatura_extraction_runs_off_loop(monkeypatch):
    seen = install_to_thread_spy(monkeypatch)
    result = await invoke_http_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="trafilatura"),
        resp=_resp,
    )
    assert result.status == "success"
    assert result.text
    assert "_extract_html" in seen


async def test_browser_trafilatura_extraction_runs_off_loop(monkeypatch):
    seen = install_to_thread_spy(monkeypatch)
    result = await invoke_browser_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="trafilatura"),
        html=_HTML_BODY.decode(),
    )
    assert result.status == "success"
    assert result.text
    assert "_extract_html" in seen


async def test_trafilatura_result_unchanged_by_offload(monkeypatch):
    """Offloading must not change the extracted text (same function, new thread)."""
    # Hermetic against trafilatura's process-global body-dedup LRU: extracting
    # the same body twice in a row diverges BY DESIGN (the first call takes
    # trafilatura's output, the repeat is discarded as duplicate and falls
    # back to markdownify, which renders the <title> trafilatura drops) -- so
    # an equivalence check across two extractions would assert on dedup state,
    # not on threading. Neutralize dedup for both calls; threading is the
    # only variable under test.
    result = await invoke_http_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="trafilatura"),
        resp=_resp,
        dedupe_patch=True,
    )
    direct = _extract_html(_HTML_BODY.decode(), "http://example.com/")
    assert result.text == (direct.text or "")
