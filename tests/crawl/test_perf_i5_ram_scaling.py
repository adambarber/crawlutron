"""Perf item 5 (RED): the per-page body cap must use the real concurrency.

``_effective_max_content_length`` scales the cap down as concurrency rises,
but ``_http_strategy_result`` never passes its concurrency, so the cap always
falls back to the settings floor (default 4 -> 10 MiB) while real dispatch
(recursive ramp, wide batches) runs far higher. The actual concurrency must
be threaded from the engine's active settings to the call site.
"""

EXPECTED_SCALED = (1024**3) // 512  # RAM budget (1 GiB) / concurrency
DEFAULT_CAP = 10 * 1024 * 1024


def _policy():
    return {
        "strategies": ["HTTP_DECLARED_CRAWLER"],
        "extraction": "full",
        "headers": {},
        "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
    }


def _page_resp(url):
    class Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        body = b"<html><body><p>Hello world, enough text to render here.</p></body></html>"
        final_url = url
        elapsed_ms = 2

    return Resp()


async def test_http_strategy_result_scales_cap_with_passed_concurrency():
    """Explicit concurrency reaches the fetcher as a scaled-down cap."""
    from app.crawl.strategies.http import _http_strategy_result

    seen: list[int] = []

    async def fetcher(url, headers, timeout, max_content_length=0):
        seen.append(max_content_length)
        return _page_resp(url)

    result = await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy=_policy(),
        http_fetcher=fetcher,
        global_concurrency=512,
    )
    assert result.status == "success"
    assert seen == [EXPECTED_SCALED]


async def test_http_strategy_result_default_preserves_settings_floor():
    """No concurrency passed -> old settings-floor behavior (10 MiB default)."""
    from app.crawl.strategies.http import _http_strategy_result

    seen: list[int] = []

    async def fetcher(url, headers, timeout, max_content_length=0):
        seen.append(max_content_length)
        return _page_resp(url)

    result = await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy=_policy(),
        http_fetcher=fetcher,
    )
    assert result.status == "success"
    assert seen == [DEFAULT_CAP]


async def test_crawl_request_threads_active_concurrency_to_fetcher():
    """End to end: the crawl's own settings.global_concurrency sizes the cap."""
    from app.crawl.engine import crawl_request
    from app.settings import CrawlerSettings

    seen: list[int] = []

    async def fetcher(url, headers, timeout, max_content_length=0):
        if url.endswith("/robots.txt"):

            class RobotsResp:
                status_code = 404
                headers = {}
                body = b""
                final_url = url
                elapsed_ms = 1

            return RobotsResp()
        seen.append(max_content_length)
        return _page_resp(url)

    settings = CrawlerSettings(global_concurrency=512)
    result = await crawl_request(
        {"urls": ["http://example.com/"]},
        settings=settings,
        http_fetcher=fetcher,
        cffi_fetcher=fetcher,
        browser_worker=None,
    )
    assert len(result["results"]) == 1
    assert seen == [EXPECTED_SCALED]
