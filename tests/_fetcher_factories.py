from typing import Any
from urllib.parse import urlparse

from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.engine import crawl_request
from app.models import CrawlResult, HttpFetchResult

SCOPE_KEEP_DROP_PAGES = {
    "/": (
        200,
        '<html><body><a href="/keep/x">k</a><a href="/drop/y">d</a></body></html>',
        "text/html",
    ),
    "/keep/x": (200, "<html>k</html>", "text/html"),
    "/drop/y": (200, "<html>d</html>", "text/html"),
}


def site_fetcher(pages: dict[str, Any]):
    def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        path = urlparse(url).path or "/"
        entry = pages.get(path)
        if entry is None:
            return make_fetch(
                status=404,
                body=b"<html></html>",
                ctype="text/html",
                url=url,
                final_url=url,
            )
        status, body, ctype = entry
        if isinstance(body, str):
            body = body.encode("utf-8")
        return make_fetch(status=status, body=body, ctype=ctype, url=url, final_url=url)

    return fetcher


async def run_scoped_crawl(scope_regex=None, *, max_depth=1, max_pages=10, pages=None):
    from app.crawl.cache import HttpCache
    from app.crawl.engine import crawl_request

    pages = pages if pages is not None else SCOPE_KEEP_DROP_PAGES
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": max_depth, "max_pages": max_pages}, "scope_regex": scope_regex},
    }
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    return {r["normalized_url"] for r in out["results"]}


def make_fetch(
    status=200,
    body=b"<html><body>hello</body></html>",
    ctype="text/html; charset=utf-8",
    final_url="http://example.com/",
    url="http://example.com/",
    headers=None,
    decompression_failed=False,
):
    return HttpFetchResult(
        url=url,
        final_url=final_url,
        status_code=status,
        headers={"content-type": ctype} if headers is None else headers,
        body=body,
        elapsed_ms=1,
        decompression_failed=decompression_failed,
    )


def ok_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
    return make_fetch(url=url)


def raise_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
    raise ConnectionError("network down")


def make_crawl_payload(
    url: str,
    *,
    strategies: list[str] | None = None,
    timeout_seconds: int = 10,
    max_retries: int = 0,
    max_discovered_links: int = 50,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": url,
        "policy": {
            "strategies": strategies if strategies is not None else [HTTP_DECLARED_CRAWLER],
            "limits": {
                "timeout_seconds": timeout_seconds,
                "max_retries": max_retries,
                "max_discovered_links": max_discovered_links,
            },
        },
    }
    payload.update(extra)
    return payload


def build_payload(
    *,
    url: str | None = None,
    urls: list[str] | None = None,
    strategies: list[str] | None = None,
    limits: dict | None = None,
    **extra,
) -> dict:
    payload = {}
    if url:
        payload["url"] = url
    if urls:
        payload["urls"] = urls
    if strategies is not None:
        payload["policy"] = {"strategies": strategies}
    if limits is not None:
        payload.setdefault("policy", {})["limits"] = limits
    payload.update(extra)
    return payload


LLM_FORMAT_TEST_HTML = (
    "<html lang='en'><head><title>Test Page</title>"
    "<meta content='no-name-attribute'>"
    "<meta name='description' content=''>"
    "<meta name='description' content='A test page'>"
    "<meta property='og:title' content='OG Title'>"
    "</head><body><main>Content here</main></body></html>"
)


async def run_crawl_async(payload: dict, *, cache=None, browser_worker=None) -> dict:
    if cache is None:
        cache = HttpCache()
    return await crawl_request(payload, browser_worker=browser_worker, cache=cache)


async def crawl_homepage_async(
    base_url: str,
    *,
    strategies: list[str] | None = None,
    limits: dict | None = None,
    cache=None,
    browser_worker=None,
) -> dict:
    payload = build_payload(
        url=f"{base_url}/",
        strategies=strategies if strategies is not None else [HTTP_DECLARED_CRAWLER],
        limits=limits
        if limits is not None
        else {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
    )
    return await run_crawl_async(payload, cache=cache, browser_worker=browser_worker)


def make_content_cache(tmpdir: str, *, default_ttl: float = 1, clock=None) -> ContentCache:
    if clock is None:
        import time

        clock = time.monotonic
    return ContentCache(tmpdir, default_ttl=default_ttl, clock=clock)


def make_content_cache_with_entry(
    tmpdir: str, key: str = "key1", value: dict = None, *, default_ttl: float = 1, clock=None
) -> ContentCache:
    if value is None:
        value = {"data": "1"}
    cache = make_content_cache(tmpdir, default_ttl=default_ttl, clock=clock)
    cache.set(key, value)
    return cache


def _temp_cache(ttl=1, clock_start=1000.0):
    import tempfile

    from app.crawl.content_cache import ContentCache

    clock_vals = [clock_start]
    return ContentCache(tempfile.mkdtemp(), default_ttl=ttl, clock=lambda: clock_vals[0]), clock_vals


def _spawn_fetch(fn):
    from helpers.server import spawn_server

    server = spawn_server()
    try:
        return fn(server)
    finally:
        server.stop()


async def invoke_http_strategy(
    monkeypatch: Any,
    *,
    policy: dict[str, Any],
    resp: Any,
    dedupe_patch: bool = False,
    global_concurrency: int | None = None,
    cap_capture: list[int] | None = None,
) -> Any:
    from app.crawl.strategies.http import _http_strategy_result

    if dedupe_patch:
        import trafilatura.core

        monkeypatch.setattr(trafilatura.core, "duplicate_test", lambda *a, **k: False)

    async def fetcher(url, headers, timeout, max_content_length=0):
        if cap_capture is not None:
            cap_capture.append(max_content_length)
        return resp(url)

    kwargs: dict[str, Any] = {}
    if global_concurrency is not None:
        kwargs["global_concurrency"] = global_concurrency
    from app.validation import HostPolicy

    return await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        host_policy=HostPolicy.unrestricted(),
        http_fetcher=fetcher,
        **kwargs,
    )


async def invoke_browser_strategy(
    monkeypatch: Any, *, policy: dict[str, Any], html: str, text: str = "Hello world"
) -> Any:
    from app.crawl.strategies.browser_result import browser_strategy_result
    from app.validation import HostPolicy

    async def browser_worker(job):
        return CrawlResult(
            url=job.url,
            status="success",
            classification="rendered",
            final_url=job.url,
            html=html,
            text=text,
            attempts=1,
            elapsed_ms=5,
            domain="example.com",
            metadata={},
        )

    return await browser_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="BROWSER_HEADLESS",
        policy=policy,
        browser_worker=browser_worker,
        host_policy=HostPolicy.unrestricted(),
    )


def _fake_browser(html=None, text="doc", metadata=None, status="success", classification="rendered"):
    def worker(job):
        return CrawlResult(
            url=job.url,
            status=status,
            classification=classification,
            final_url=job.url,
            html=html,
            text=text,
            attempts=1,
            elapsed_ms=1,
            domain="x",
            metadata=metadata or {},
        )

    return worker


__all__ = [
    "LLM_FORMAT_TEST_HTML",
    "SCOPE_KEEP_DROP_PAGES",
    "_fake_browser",
    "_spawn_fetch",
    "_temp_cache",
    "build_payload",
    "crawl_homepage_async",
    "invoke_browser_strategy",
    "invoke_http_strategy",
    "make_content_cache",
    "make_content_cache_with_entry",
    "make_crawl_payload",
    "make_fetch",
    "ok_fetcher",
    "raise_fetcher",
    "run_crawl_async",
    "run_scoped_crawl",
    "site_fetcher",
]
