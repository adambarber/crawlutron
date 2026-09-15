"""Perf item 3 (RED): sync trafilatura extraction must leave the event loop.

``_http_strategy_result`` (strategies/http.py) and ``_browser_strategy_result``
(engine.py) call ``_extract_html`` (trafilatura, CPU-bound) synchronously
inside async functions. Both must go through ``asyncio.to_thread`` like the
neighboring ``_extract_document``/``extract_content`` calls already do.
"""

import asyncio

from app.crawl.extraction import _extract_html


def _policy(extraction="trafilatura"):
    return {
        "strategies": ["HTTP_DECLARED_CRAWLER"],
        "extraction": extraction,
        "headers": {},
        "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
    }


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


def _install_to_thread_spy(monkeypatch):
    """Record every function offloaded via asyncio.to_thread; still run it."""
    import functools

    seen: list[str] = []
    orig = asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        if isinstance(fn, functools.partial):
            seen.append(getattr(fn.func, "__name__", repr(fn.func)))
        else:
            seen.append(getattr(fn, "__name__", repr(fn)))
        return await orig(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    return seen


async def test_http_trafilatura_extraction_runs_off_loop(monkeypatch):
    from app.crawl.strategies.http import _http_strategy_result

    seen = _install_to_thread_spy(monkeypatch)

    async def fetcher(url, headers, timeout, max_content_length=0):
        return _resp(url)

    result = await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy=_policy("trafilatura"),
        http_fetcher=fetcher,
    )
    assert result.status == "success"
    assert result.text
    assert "_extract_html" in seen


async def test_browser_trafilatura_extraction_runs_off_loop(monkeypatch):
    from app.crawl.engine import _browser_strategy_result
    from app.models import CrawlResult

    seen = _install_to_thread_spy(monkeypatch)

    async def browser_worker(job):
        return CrawlResult(
            url=job.url,
            status="success",
            classification="rendered",
            final_url=job.url,
            html=_HTML_BODY.decode(),
            text="Hello world",
            attempts=1,
            elapsed_ms=5,
            domain="example.com",
            metadata={},
        )

    result = await _browser_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="BROWSER_HEADLESS",
        policy=_policy("trafilatura"),
        browser_worker=browser_worker,
    )
    assert result.status == "success"
    assert result.text
    assert "_extract_html" in seen


async def test_trafilatura_result_unchanged_by_offload(monkeypatch):
    """Offloading must not change the extracted text (same function, new thread)."""
    from app.crawl.strategies.http import _http_strategy_result

    async def fetcher(url, headers, timeout, max_content_length=0):
        return _resp(url)

    result = await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy=_policy("trafilatura"),
        http_fetcher=fetcher,
    )
    direct = _extract_html(_HTML_BODY.decode(), "http://example.com/")
    assert result.text == (direct.text or "")
