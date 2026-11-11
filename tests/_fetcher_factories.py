"""Sanctioned fetch fakes; definitions live in the sibling helper modules.

Fetch doubles are imported here only when the bodies need them; the single
import door for a fetch double is ``tests._fetch_doubles`` (#314, 3.3)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from crawlutron.admission.policy import CrawlPolicy
from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.json_value import JsonValue
from tests._browser_doubles import _fake_browser

if TYPE_CHECKING:
    from crawlutron.models.crawl_result import EngineCrawlResult
from tests._clocks import clocked_cache, make_seeded_cache
from tests._crawl_runners import (
    LINKED_PAIR_LIMITS,
    LLM_FORMAT_TEST_HTML,
    _CrawlHttpKwargs,
    _spawn_fetch,
    build_payload,
    cache_with_etag,
    crawl_homepage_async,
    crawled_result,
    crawled_results,
    crawled_urls,
    make_crawl_payload,
    run_cached_twice,
    run_chain,
    run_crawl_async,
    run_http_strategy,
    site_linking_to,
    strategy_request,
    xml_page,
)
from tests._fetch_doubles import (
    FetcherDouble,
)
from tests._policy_factories import canonical


async def invoke_http_strategy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: CrawlPolicy | dict[str, JsonValue],
    resp: Callable[[object], HttpFetchResult],
    dedupe_patch: bool = False,
    global_concurrency: int | None = None,
    cap_capture: list[int] | None = None,
) -> EngineCrawlResult:
    if dedupe_patch:
        # Lazy on purpose, and the reason is measured: `import trafilatura` costs roughly 400ms because it
        # transitively pulls dateparser, ua_parser, browserforge, onnxruntime, mammoth, lxml and PIL.
        import trafilatura.core

        monkeypatch.setattr(trafilatura.core, "duplicate_test", lambda *a, **k: False)

    def answer(url, _sent_headers, *_):
        return resp(url)

    fetcher_double = FetcherDouble(answer, record_cap=cap_capture, answer_is_response=True)
    kwargs: _CrawlHttpKwargs = {}
    if global_concurrency is not None:
        kwargs["global_concurrency"] = global_concurrency
    return await run_http_strategy(
        url="http://example.com/",
        policy=policy,
        http_fetcher=fetcher_double,
        **kwargs,
    )


async def invoke_browser_strategy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: CrawlPolicy | dict[str, JsonValue],
    html: str,
    text: str = "Hello world",
) -> EngineCrawlResult:
    from crawlutron.transport.browser_result import browser_strategy_result

    return await browser_strategy_result(
        request=strategy_request("http://example.com/", policy, "BROWSER_HEADLESS"),
        browser_worker=_fake_browser(html=html, text=text),
    )


async def run_default_http_fetcher(url: str, headers=None, *, pin=None):
    """The real default fetcher against a caller-supplied pin rule; ``pin`` is the
    fetcher's own seam, forwarded rather than rebound over the transport module's
    private builder, because a redirect test needs a pin that is not a resolver.
    """
    from crawlutron.transport.fetchers import default_http_fetcher

    return await default_http_fetcher(
        canonical(url),
        {} if headers is None else headers,
        30,
        pin=pin,
    )


def make_expiring_cache(tmpdir, *, max_total_bytes: int = 10_000):
    """Two entries that expire at the same instant, and the clock to expire them.

    A test about TTL and byte accounting needs a budget it can overrun and two
    entries it can age out, which is the pair of values the loop below seeds.
    """
    return make_seeded_cache(
        tmpdir,
        "a",
        "b",
        value="x" * 100,
        default_ttl=10,
        max_entries=100,
        max_total_bytes=max_total_bytes,
    )


__all__ = [
    "LLM_FORMAT_TEST_HTML",
    "_spawn_fetch",
    "build_payload",
    "cache_with_etag",
    "crawl_homepage_async",
    "crawled_result",
    "crawled_results",
    "crawled_urls",
    "clocked_cache",
    "invoke_browser_strategy",
    "invoke_http_strategy",
    "make_crawl_payload",
    "make_expiring_cache",
    "make_seeded_cache",
    "site_linking_to",
    "run_cached_twice",
    "run_chain",
    "run_crawl_async",
    "run_default_http_fetcher",
    "run_http_strategy",
    "LINKED_PAIR_LIMITS",
    "xml_page",
]
