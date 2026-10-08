"""The per-page content-length body cap must scale with real concurrency.

``_effective_max_content_length`` scales the cap down as concurrency rises,
but ``http_strategy_result`` never passes its concurrency, so the cap always
falls back to the settings floor (default 4 -> 10 MiB) while real dispatch
(recursive ramp, wide batches) runs far higher. The actual concurrency must
be threaded from the engine's active settings to the call site.
"""

import pytest

from tests._fetcher_factories import invoke_http_strategy
from tests._policy_factories import http_strategy_policy

EXPECTED_SCALED = (1024**3) // 512  # RAM budget (1 GiB) / concurrency
DEFAULT_CAP = 10 * 1024 * 1024


def _page_resp(url):
    class Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        body = b"<html><body><p>Hello world, enough text to render here.</p></body></html>"
        final_url = url
        elapsed_ms = 2

    return Resp()


@pytest.mark.parametrize(
    ("global_concurrency", "expected_cap"),
    [
        (512, EXPECTED_SCALED),
        (None, DEFAULT_CAP),
    ],
)
async def test_http_strategy_result_cap_follows_concurrency(monkeypatch, global_concurrency, expected_cap):
    """Explicit concurrency scales the cap; omitted keeps the settings floor."""
    seen: list[int] = []
    result = await invoke_http_strategy(
        monkeypatch,
        policy=http_strategy_policy(extraction="full"),
        resp=_page_resp,
        global_concurrency=global_concurrency,
        cap_capture=seen,
    )
    assert result.status == "success"
    assert seen == [expected_cap]


async def test_crawl_request_threads_active_concurrency_to_fetcher():
    """End to end: the crawl's own settings.global_concurrency sizes the cap."""
    from app.config.settings import CrawlerSettings
    from app.orchestration.engine import crawl_request

    seen: list[int] = []

    async def fetcher(url, headers, timeout, max_content_length=0):
        if url.path == "/robots.txt":

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
