"""Shared test helpers for the browser-crawler test suite.

Centralizes the small factories/fixtures that were previously copy-pasted
across several test modules (``site_fetcher``, ``block_private_targets``,
``make_job``, ``make_result``) so the suite stays DRY and jscpd reports no
duplication.
"""

from typing import Any
from urllib.parse import urlparse

from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl.strategies.http import HttpFetchResult


def block_private_targets(monkeypatch) -> None:
    """Disable the local-dev private-target escape hatch for one test.

    Uses ``monkeypatch`` so the environment is restored automatically and never
    poisons sibling tests.
    """
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")


def site_fetcher(pages: dict[str, Any]):
    """Build a sync fetcher serving an in-memory ``path -> (status, body, ctype)`` map."""

    def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        path = urlparse(url).path or "/"
        entry = pages.get(path)
        if entry is None:
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=404,
                headers={"content-type": "text/html"},
                body=b"<html></html>",
                elapsed_ms=1,
            )
        status, body, ctype = entry
        if isinstance(body, str):
            body = body.encode("utf-8")
        return HttpFetchResult(
            url=url,
            final_url=url,
            status_code=status,
            headers={"content-type": ctype},
            body=body,
            elapsed_ms=1,
        )

    return fetcher


def make_job(index: int, url: str):
    from app.models import CrawlJob

    return CrawlJob(index=index, url=url, domain=url.split("/")[2], options=None)


def make_result(url: str, domain: str):
    from app.models import CrawlStrategyAttempt, EngineCrawlResult

    return EngineCrawlResult(
        original_url=url,
        normalized_url=url,
        final_url=url,
        status="success",
        classification="rendered",
        selected_strategy="HTTP_DECLARED_CRAWLER",
        attempted_strategies=[
            CrawlStrategyAttempt(
                strategy="HTTP_DECLARED_CRAWLER",
                status="success",
                classification="rendered",
                http_status=200,
                content_type="text/html",
                elapsed_ms=0,
                error=None,
            )
        ],
        http_status=200,
        headers={},
        content_type="text/html",
        html="<html></html>",
        text="ok",
        markdown="ok",
        extraction_method="http_html",
        quality_score=1.0,
        classification_evidence=[],
        diagnostics={},
        discovered_link_count=0,
        discovered_links_truncated=False,
        discovered_links=[],
        error=None,
        next_page_url=None,
    )


def assert_dns_pinning_uses_first_resolution(monkeypatch, rebound_ip, public_ip="93.184.216.34") -> None:
    """Assert ``_resolve_and_pin_host`` pins the first resolution even if a
    second resolution would rebound to ``rebound_ip`` (DNS-rebinding / TOCTOU guard)."""
    import socket
    from unittest.mock import patch

    from app.crawl.strategies.http import _resolve_and_pin_host

    block_private_targets(monkeypatch)
    calls = []

    def fake_getaddrinfo(host, *a, **k):
        calls.append(host)
        if len(calls) == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (public_ip, 80))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (rebound_ip, 80))]

    with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
        hostname, ip = _resolve_and_pin_host("http://example.com/")
    assert ip == public_ip
    assert calls == ["example.com"], "hostname should be resolved exactly once"


# Shared fixture for scope-filtering tests: a site with one in-scope ("/keep/")
# link and one out-of-scope ("/drop/") link.
SCOPE_KEEP_DROP_PAGES = {
    "/": (
        200,
        '<html><body><a href="/keep/x">k</a><a href="/drop/y">d</a></body></html>',
        "text/html",
    ),
    "/keep/x": (200, "<html>k</html>", "text/html"),
    "/drop/y": (200, "<html>d</html>", "text/html"),
}


async def run_scoped_crawl(scope_regex=None, *, max_depth=1, max_pages=10, pages=None):
    """Drive a single-level scoped recursive crawl and return the result URLs.

    Used by scope-filtering tests so the site fixture and crawl invocation live
    in one place (keeps the suite DRY / jscpd-clean).
    """
    from app.crawl.cache import HttpCache
    from app.crawl.engine import crawl_request

    pages = pages if pages is not None else SCOPE_KEEP_DROP_PAGES
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": max_depth, "max_pages": max_pages}, "scope_regex": scope_regex},
    }
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    return {r["normalized_url"] for r in out["results"]}


# --- Shared fetcher mocks (single source of truth) ---


def make_fetch(
    status=200,
    body=b"<html><body>hello</body></html>",
    ctype="text/html; charset=utf-8",
    final_url="http://example.com/",
    url="http://example.com/",
    headers=None,
    decompression_failed=False,
):
    """The sanctioned HttpFetchResult fake.

    Tests must not hand-build the frozen dataclass (#1797): a new field
    (like ``decompression_failed``) lands once here instead of rippling
    through every fixture. ``headers`` overrides the ctype shorthand when a
    test needs conditional/encoding headers.
    """
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


# --- Shared minimal crawl policy ---


def make_base_policy(strategies=None) -> dict:
    """Fresh copy of the minimal crawl policy used across suites.

    Returns a new dict each call so tests can mutate it without leaking
    state into sibling tests.
    """
    return {
        "schema_version": "2026-06-23",
        "strategies": list(strategies) if strategies is not None else [HTTP_DECLARED_CRAWLER],
        "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
        "headers": {},
        "request_profile": {},
        "browser_profile": {},
        "discovery": {},
    }


# --- EngineCrawlResult assertion helpers ("matchers") ---


def assert_result_success(result, classification: str = "rendered") -> None:
    """Assert an ``EngineCrawlResult`` succeeded with the expected classification."""
    assert result.status == "success"
    assert result.classification == classification


def assert_result_failed(result, classification: str) -> None:
    """Assert an ``EngineCrawlResult`` failed with the expected classification."""
    assert result.status == "failed"
    assert result.classification == classification


# --- Shared browser-render test doubles ---


class BrowserFakePage:
    """Strict Playwright-like fake: rejects extra_http_headers kwarg in goto
    (real API raises TypeError) and records route/header/goto calls."""

    def __init__(self):
        self.goto_calls = []
        self.route_calls = []
        self.set_extra_http_headers_calls = []
        self.url = "http://1.2.3.4/"

    async def route(self, pattern, handler):
        # Real (async) Playwright page.route is a coroutine.
        self.route_calls.append((pattern, handler))

    def on(self, event, handler):
        pass

    async def goto(self, url, wait_until="load", **kwargs):
        if "extra_http_headers" in kwargs:
            raise TypeError("unexpected keyword argument 'extra_http_headers'")
        self.goto_calls.append((url, kwargs))

    async def set_extra_http_headers(self, headers):
        self.set_extra_http_headers_calls.append(headers)

    async def evaluate(self, *a, **k):
        return ""

    async def close(self):
        pass

    async def screenshot(self, **k):
        return b""

    async def query_selector(self, *a, **k):
        return None


class BrowserFakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_page(self, **kwargs):
        return self._page


async def _browser_noop(self, *a, **k):
    return None


def _async_return(value):
    async def coro():
        return value

    return coro()


def build_pinned_renderer(monkeypatch, page):
    """BrowserRenderer whose transport is replaced by ``page`` (no browser)."""
    from app.crawl.strategies.browser import BrowserRenderer

    renderer = BrowserRenderer(warmup_urls=[])

    async def fake_acquire(self, options):
        return BrowserFakeBrowser(page)

    monkeypatch.setattr(BrowserRenderer, "_acquire_browser", fake_acquire)
    monkeypatch.setattr(BrowserRenderer, "_get_page_kwargs", lambda self, prof: {})
    monkeypatch.setattr(BrowserRenderer, "_wait_for_ready_state", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_run_action_chain", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_lazy_load_scroll", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_flatten_shadow_dom", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_auto_accept_cookie_consent", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_safe_close_page", _browser_noop)
    monkeypatch.setattr(
        BrowserRenderer,
        "_extract_with_challenge_wait",
        lambda self, page, options: _async_return(("<html>hi</html>", "hi", "ok", False)),
    )
    monkeypatch.setattr(BrowserRenderer, "_looks_like_pdf", lambda self, url: False)
    monkeypatch.setattr(BrowserRenderer, "_status_for_classification", lambda self, c: "success")
    return renderer


def pinned_renderer(monkeypatch, *, pinned=("http://1.2.3.4:80/", {"Host": "example.com"})):
    """BrowserFakePage + pinned renderer wired for Host-override navigation.

    The Host-override navigation tests share this setup: ``_build_pinned_request``
    returns the pinned target and Host header, and the page records
    ``set_extra_http_headers`` calls for post-navigation assertions.
    """
    import app.crawl.strategies.browser as browser_mod

    page = BrowserFakePage()
    renderer = build_pinned_renderer(monkeypatch, page)
    monkeypatch.setattr(browser_mod, "_build_pinned_request", lambda url, headers: pinned)
    return page, renderer


def provider_results(*items) -> str:
    """JSON body for ``fake_provider_http`` shaped like a keyed-provider response."""
    import json

    return json.dumps({"results": list(items)})


async def poll_until(predicate, *, timeout: float = 10.0, interval: float = 0.1, message: str | None = None):
    """Poll ``predicate`` until truthy or ``timeout`` expires.

    Async-only (the test suite runs everything under asyncio). On timeout,
    ``pytest.fail`` with an optional ``message`` so the test names the
    failing condition in its traceback.
    """
    import asyncio

    import pytest

    async def _runner() -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return True
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(interval)

    try:
        ok = await asyncio.wait_for(_runner(), timeout=timeout + 1.0)
    except TimeoutError:
        ok = False
    if not ok:
        pytest.fail(message or f"poll_until timed out after {timeout}s")
    return True


def make_crawl_payload(
    url: str,
    *,
    strategies: list[str] | None = None,
    timeout_seconds: int = 10,
    max_retries: int = 0,
    max_discovered_links: int = 50,
    **extra: Any,
) -> dict[str, Any]:
    """The sanctioned /v1/crawl request-payload builder.

    The default strategy/limits triple was hand-copied per API test file and
    already risked silent divergence; a new default lands once here. Callers
    with intentional variants (e.g. cookie tests pinning max_discovered_links)
    pass them explicitly. ``extra`` carries endpoint flags like async_mode.
    """
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


def extract_function_body(source: str, name: str) -> str:
    """Extract just the body of an async function ``name`` at any indent.

    AST-text tests (source-fingerprint assertions) share this helper so the
    regex contract lives in one place instead of per test file.
    """
    import re

    pattern = re.compile(
        r"^[ \t]*async def "
        + re.escape(name)
        + r"\b.*?^[ \t]*(?=@app\.middleware|class |def |async def [a-zA-Z]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(source)
    if not match:
        raise AssertionError(f"Could not find function {name}")
    return match.group(0)


class MockResponse:
    """Scripted httpx streaming response for ``_default_http_fetcher`` tests."""

    def __init__(self, status_code, headers, content=b""):
        self.status_code = status_code
        self.headers = headers
        self._content = content
        self.url = "http://example.com/"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_raw(self):
        yield self._content


class MockClient:
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0
        self.captured_headers = []

    def stream(self, method, url, headers, timeout, follow_redirects):
        self.call_count += 1
        self.captured_headers.append(dict(headers))
        resp = self.responses[self.call_count - 1]
        resp.url = url
        return resp


def patch_http_fetcher_transport(monkeypatch, responses):
    """Route ``_default_http_fetcher`` through a scripted mock client.

    Neutralizes SSRF/DNS so the fetcher can be driven against canned responses
    (per-hop headers land in ``MockClient.captured_headers``).
    """
    import app.crawl.strategies.http as http_mod

    mock_client = MockClient(responses)
    monkeypatch.setattr(http_mod, "get_async_http_client", lambda: mock_client)
    monkeypatch.setattr(http_mod, "_check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(
        http_mod,
        "_resolve_and_pin_host",
        lambda url: (urlparse(url).hostname, "1.2.3.4"),
    )
    return mock_client
