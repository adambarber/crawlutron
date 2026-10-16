"""Sanctioned fetch fakes; definitions live in the sibling helper modules."""

from typing import Any

from app.admission.policy import CrawlPolicy
from tests._browser_doubles import _fake_browser
from tests._crawl_runners import (
    LINKED_PAIR_LIMITS,
    LLM_FORMAT_TEST_HTML,
    _spawn_fetch,
    build_payload,
    cache_with_etag,
    clocked_cache,
    crawl_homepage_async,
    crawled_result,
    crawled_results,
    crawled_urls,
    make_crawl_payload,
    make_seeded_cache,
    run_cached_twice,
    run_chain,
    run_crawl_async,
    run_http_strategy,
    site_linking_to,
    strategy_request,
    xml_page,
)
from tests._fetch_doubles import (
    SCOPE_KEEP_DROP_PAGES,
    FetcherDouble,
    Route,
    failing_fetcher,
    make_fetch,
    ok_fetcher,
    raise_fetcher,
    run_scoped_crawl,
    scripted_fetcher,
    site_fetcher,
)
from tests._fetch_scripted import (
    concurrent_fetcher,
    hanging_fetcher,
    request_fetcher,
    routed_fetcher,
    sequenced_fetcher,
    tap_fetcher,
)
from tests._policy_factories import canonical


async def invoke_http_strategy(
    monkeypatch: Any,
    *,
    policy: CrawlPolicy | dict[str, Any],
    resp: Any,
    dedupe_patch: bool = False,
    global_concurrency: int | None = None,
    cap_capture: list[int] | None = None,
) -> Any:
    if dedupe_patch:
        # Lazy on purpose, and the reason is measured: `import trafilatura` costs roughly 400ms because it
        # transitively pulls dateparser, ua_parser, browserforge, onnxruntime, mammoth, lxml and PIL.
        import trafilatura.core

        monkeypatch.setattr(trafilatura.core, "duplicate_test", lambda *a, **k: False)

    def answer(url, _sent_headers, *_):
        return resp(url)

    kwargs: dict[str, Any] = {}
    if global_concurrency is not None:
        kwargs["global_concurrency"] = global_concurrency
    return await run_http_strategy(
        url="http://example.com/",
        policy=policy,
        http_fetcher=FetcherDouble(answer, record_cap=cap_capture, answer_is_response=True),
        **kwargs,
    )


async def invoke_browser_strategy(
    monkeypatch: Any, *, policy: CrawlPolicy | dict[str, Any], html: str, text: str = "Hello world"
) -> Any:
    from app.transport.browser_result import browser_strategy_result

    return await browser_strategy_result(
        request=strategy_request("http://example.com/", policy, "BROWSER_HEADLESS"),
        browser_worker=_fake_browser(html=html, text=text),
    )


def make_html_fetcher(calls: list[str], body: bytes = b"<html><body>page content</body></html>"):
    """A fetcher serving one HTML body for any URL, recording what it was asked for."""
    return FetcherDouble(lambda url, *_: {"status": 200, "ctype": "text/html", "body": body}, calls)


async def run_default_http_fetcher(url: str, headers=None, *, pin=None):
    """The real default fetcher against a caller-supplied pin rule; ``pin`` is the
    fetcher's own seam, forwarded rather than rebound over the transport module's
    private builder, because a redirect test needs a pin that is not a resolver.
    """
    from app.transport.fetchers import default_http_fetcher

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
    "SCOPE_KEEP_DROP_PAGES",
    "_spawn_fetch",
    "build_payload",
    "cache_with_etag",
    "crawl_homepage_async",
    "crawled_result",
    "crawled_results",
    "crawled_urls",
    "clocked_cache",
    "FetcherDouble",
    "invoke_browser_strategy",
    "invoke_http_strategy",
    "make_crawl_payload",
    "make_expiring_cache",
    "make_fetch",
    "make_html_fetcher",
    "make_seeded_cache",
    "failing_fetcher",
    "concurrent_fetcher",
    "hanging_fetcher",
    "ok_fetcher",
    "raise_fetcher",
    "site_linking_to",
    "Route",
    "request_fetcher",
    "routed_fetcher",
    "run_cached_twice",
    "run_chain",
    "scripted_fetcher",
    "sequenced_fetcher",
    "tap_fetcher",
    "run_crawl_async",
    "run_default_http_fetcher",
    "run_http_strategy",
    "run_scoped_crawl",
    "site_fetcher",
    "LINKED_PAIR_LIMITS",
    "xml_page",
]
