"""Shared test helpers for the browser-crawler test suite.

Centralizes the small factories/fixtures that were previously copy-pasted
across several test modules (``site_fetcher``, ``block_private_targets``,
``make_job``, ``make_result``) so the suite stays DRY and jscpd reports no
duplication.
"""

import asyncio
import gzip
import os
import socket
from typing import Any
from urllib.parse import urlparse

import pytest

from app.constants import HTTP_DECLARED_CRAWLER
from app.models import HttpFetchResult
from app.validation import HostPolicy

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()

BOOL_ENV_CASES = (
    (None, True),
    ("", True),
    ("   ", True),
    ("1", True),
    (" TrUe ", True),
    ("yes", True),
    ("ON", True),
    ("0", False),
    (" FALSE ", False),
    ("No", False),
    ("off", False),
    ("garbage", True),
)


def set_optional_env(monkeypatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


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

    return CrawlJob(
        index=index,
        url=url,
        domain=url.split("/")[2],
        options=None,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )


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

    from app.crawl.strategies.pinned_transport import _resolve_and_pin_host

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
        lambda self, page, options: _async_return(("<html>hi</html>", "hi", "rendered", False)),
    )
    monkeypatch.setattr(BrowserRenderer, "_looks_like_pdf", lambda self, url: False)
    # Force the shared constants resolver (imported into the browser module's
    # own namespace) so these fakes always report success regardless of the
    # page content the fake transport returns.
    from app.crawl.strategies import browser as browser_module

    monkeypatch.setattr(browser_module, "status_for_classification", lambda c: "success")
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
    monkeypatch.setattr(browser_mod, "_build_pinned_request", lambda url, headers, *, policy: pinned)
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
    from app.crawl.strategies import http_client_pool as pool_mod
    from app.crawl.strategies import pinned_transport as pinned_mod

    mock_client = MockClient(responses)
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: mock_client)
    monkeypatch.setattr(pinned_mod, "_check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(
        pinned_mod,
        "_resolve_and_pin_host",
        lambda url: (urlparse(url).hostname, "1.2.3.4"),
    )
    return mock_client


def install_to_thread_spy(monkeypatch):
    """Record every function offloaded via ``asyncio.to_thread``; still run it."""
    import asyncio
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


def make_engine_result(url: str, links=()):
    """Minimal success ``EngineCrawlResult`` for recursive/dispatch unit tests."""
    from app.models import EngineCrawlResult

    return EngineCrawlResult(
        original_url=url,
        normalized_url=url,
        final_url=url,
        status="success",
        classification="rendered",
        selected_strategy="HTTP_DECLARED_CRAWLER",
        attempted_strategies=[],
        http_status=200,
        headers={},
        content_type="text/html",
        html="<html></html>",
        text="t",
        extraction_method="http_html",
        quality_score=1.0,
        discovered_links=list(links),
    )


class Spill:
    """In-memory result spill for recursive-dispatch tests (same contract as
    :class:`app.crawl.engine._ResultSpill` for the test-only surface)."""

    def __init__(self):
        self.writes: dict[int, Any] = {}

    def write(self, index: int, result: Any) -> None:
        self.writes[index] = result

    def load_ordered(self) -> list[Any]:
        return [self.writes[i] for i in sorted(self.writes)]


class FakeScheduler:
    """Shared domain-scheduler double for recursive-dispatch tests.

    Runs jobs in submission order and routes a raising worker through the
    caller's ``failed_result`` envelope when supplied, matching the real
    scheduler's contract. Parameters are named explicitly (no ``**kwargs``) so
    a required production kwarg added later cannot be silently accepted and
    dropped -- the shape that hid the ``failed_result`` refactor's breakage
    across 24 tests.
    """

    def __init__(self):
        self.runs = 0
        self.pins: dict[str, str] = {}

    def get_pinned_strategy(self, domain: str):
        return self.pins.get(domain)

    async def run(self, jobs, worker, on_result=None, *, failed_result=None):
        self.runs += 1
        out = {}
        for job in jobs:
            try:
                out[job.index] = await worker(job)
            except Exception as error:
                if failed_result is None:
                    raise
                out[job.index] = failed_result(job, error)
        return out


def http_strategy_policy(*, extraction: str = "full", discovery: bool | None = None) -> dict:
    """Minimal HTTP-strategy policy for direct ``_http_strategy_result`` tests."""
    policy: dict[str, Any] = {
        "strategies": ["HTTP_DECLARED_CRAWLER"],
        "extraction": extraction,
        "headers": {},
        "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
    }
    if discovery is not None:
        policy["discovery"] = {"links": True} if discovery else {}
    return policy


async def invoke_http_strategy(
    monkeypatch: Any,
    *,
    policy: dict[str, Any],
    resp: Any,
    dedupe_patch: bool = False,
    global_concurrency: int | None = None,
    cap_capture: list[int] | None = None,
) -> Any:
    """Drive ``_http_strategy_result`` with an in-memory response object."""
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
    return await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        host_policy=UNRESTRICTED_HOST_POLICY,
        http_fetcher=fetcher,
        **kwargs,
    )


async def invoke_browser_strategy(
    monkeypatch: Any, *, policy: dict[str, Any], html: str, text: str = "Hello world"
) -> Any:
    """Drive ``_browser_strategy_result`` with an in-memory browser worker."""
    from app.crawl.strategies.browser_result import browser_strategy_result
    from app.models import CrawlResult

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
        host_policy=UNRESTRICTED_HOST_POLICY,
    )


# ---------------------------------------------------------------------------
# Factories consolidated from the former tests/helpers/__init__.py.
# _testkit is now the single home for test factories; tests/helpers/ keeps only
# the fixture HTTP server (helpers.server).
# ---------------------------------------------------------------------------

import pathlib

from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.engine import crawl_request
from app.crawl.job_store import JobStatus, JobStore


def build_payload(
    *,
    url: str | None = None,
    urls: list[str] | None = None,
    strategies: list[str] | None = None,
    limits: dict | None = None,
    **extra,
) -> dict:
    """Build a crawl request payload dict from keyword arguments."""
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


# Shared HTML fixtures to avoid duplication across test modules.
LLM_FORMAT_TEST_HTML = (
    "<html lang='en'><head><title>Test Page</title>"
    "<meta content='no-name-attribute'>"
    "<meta name='description' content=''>"
    "<meta name='description' content='A test page'>"
    "<meta property='og:title' content='OG Title'>"
    "</head><body><main>Content here</main></body></html>"
)


# Async helper for pytest-based tests
async def run_crawl_async(payload: dict, *, cache=None, browser_worker=None) -> dict:
    """Async version of run_crawl for pytest-asyncio tests."""
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
    """Crawl the local test server homepage and return the unified response dict."""
    payload = build_payload(
        url=f"{base_url}/",
        strategies=strategies if strategies is not None else [HTTP_DECLARED_CRAWLER],
        limits=limits
        if limits is not None
        else {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
    )
    return await run_crawl_async(payload, cache=cache, browser_worker=browser_worker)


def make_content_cache(tmpdir: str, *, default_ttl: float = 1, clock=None) -> ContentCache:
    """Create a ContentCache with test-friendly defaults.

    Args:
        tmpdir: Temporary directory path
        default_ttl: Default TTL in seconds (default 1)
        clock: Optional clock function for time control (default: time.monotonic)

    Returns:
        Configured ContentCache instance
    """
    if clock is None:
        import time

        clock = time.monotonic
    return ContentCache(tmpdir, default_ttl=default_ttl, clock=clock)


def make_job_store(tmpdir: str) -> JobStore:
    """Create a JobStore with test-friendly defaults.

    Args:
        tmpdir: Temporary directory path

    Returns:
        Configured JobStore instance
    """
    db_path = pathlib.Path(tmpdir) / "jobs.db"
    return JobStore(db_path, policy=UNRESTRICTED_HOST_POLICY)


async def run_job_control_test(
    app,
    job_store: JobStore,
    job_id: str,
    action: str,
    expected_status: int = 422,
) -> None:
    """Run a job control test with the given action and expected status.

    This helper encapsulates the common pattern for testing job control endpoints.

    Usage:
        job = job_store.create("http://example.com/", {})
        await run_job_control_test(app, job_store, job.job_id, "cancel", 422)
    """
    from httpx import ASGITransport, AsyncClient

    async with ASGITransport(app=app) as transport:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": action})
            assert resp.status_code == expected_status


def make_content_cache_with_entry(
    tmpdir: str, key: str = "key1", value: dict = None, *, default_ttl: float = 1, clock=None
) -> ContentCache:
    """Create a ContentCache with a test entry already set.

    This reduces duplication in ContentCache tests that all start with
    the same setup pattern.

    Args:
        tmpdir: Temporary directory path
        key: Cache key to set (default "key1")
        value: Value to store (default {"data": "1"})
        default_ttl: Default TTL in seconds (default 1)
        clock: Optional clock function for time control

    Returns:
        Configured ContentCache instance with the entry already set
    """
    if value is None:
        value = {"data": "1"}
    cache = make_content_cache(tmpdir, default_ttl=default_ttl, clock=clock)
    cache.set(key, value)
    return cache


def browser_available() -> bool:
    """Return True if a usable browser binary is installed.

    Used to skip browser-dependent tests in environments where the browser
    binary is not installed (e.g. lightweight CI images). When the binary is
    present the tests run normally.

    Checks Camoufox FIRST: BrowserRenderer launches via camoufox, so its
    binary is the capability that matters. The Playwright Chromium check
    remains as a fallback (it version-drifts independently of camoufox).
    """
    import os

    try:
        from camoufox.utils import launch_path

        return bool(launch_path()) and os.path.exists(launch_path())
    except Exception:
        pass
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False

    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            return bool(path) and os.path.exists(path)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Coverage-suite surface, consolidated from the former tests/coverage_helpers.py.
#
# Star-import suites (``from _testkit import *``) exercise app internals --
# including underscore-prefixed functions -- so ``__all__`` below re-exports
# the whole module namespace and those tests keep resolving the same names.
# ---------------------------------------------------------------------------

# allow loopback crawl targets in this process too
os.environ.setdefault("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")

from helpers.server import spawn_server

from app.api.render import render_request
from app.classify import classify_page_with_evidence
from app.constants import (
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
)
from app.crawl.decompression import decompress_body
from app.crawl.document_text import (
    _collect_document_item_lines,
    _document_item_range,
    _format_extracted_document_text,
    _is_document_item_marker,
    _is_document_non_item_section,
    _join_document_paragraph_lines,
    _looks_like_document_heading,
    _next_non_empty_document_line,
    _should_join_document_paragraph,
)
from app.crawl.engine import _payload_policy
from app.crawl.extraction import (
    _detect_pagination as _detect_pagination_with_policy,
)
from app.crawl.extraction import (
    _extract_discovered_links as _extract_discovered_links_with_policy,
)
from app.crawl.extraction import (
    _html_diagnostics as _html_diagnostics_with_policy,
)
from app.crawl.extraction import (
    extract_content as _extract_content_with_policy,
)
from app.crawl.extraction import (
    extract_js_endpoints as _extract_js_endpoints_with_policy,
)
from app.crawl.llm_format import _extract_llm_metadata
from app.crawl.strategies.body_decode import _decode_body, _quality_score
from app.crawl.strategies.document import (
    _default_document_text_extractor as _default_document_text_extractor_with_policy,
)
from app.crawl.strategies.document import (
    _format_table,
)
from app.crawl.strategies.fetchers import _default_http_fetcher as _default_http_fetcher_with_policy
from app.crawl.strategies.request_profile import _cookie_header_for_url, _headers_for_strategy
from app.crawl.strategy_chain import execute_crawl_chain
from app.models import CrawlJob, CrawlResult, RenderOptions, to_dict
from app.validation import (
    get_env_int,
    registrable_domain,
)
from app.validation import (
    normalize_public_http_url as _normalize_public_http_url,
)
from app.validation import (
    public_url_or_none as _public_url_or_none,
)


def normalize_public_http_url(url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _normalize_public_http_url(url, policy=policy)


def public_url_or_none(raw, *, resolve_against=None, policy=UNRESTRICTED_HOST_POLICY):
    return _public_url_or_none(
        raw,
        resolve_against=resolve_against,
        policy=policy,
    )


def _html_diagnostics(html, source_url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _html_diagnostics_with_policy(html, source_url, host_policy=policy)


def extract_content(
    html,
    content_type,
    source_url,
    css_selector=None,
    xpath=None,
):
    return _extract_content_with_policy(
        html,
        content_type,
        source_url,
        css_selector,
        xpath,
    )


def extract_js_endpoints(endpoints, source_url=None, *, policy=UNRESTRICTED_HOST_POLICY):
    return _extract_js_endpoints_with_policy(
        endpoints,
        source_url,
        host_policy=policy,
    )


def _detect_pagination(html, final_url, *, policy=UNRESTRICTED_HOST_POLICY):
    return _detect_pagination_with_policy(html, final_url, host_policy=policy)


def _extract_discovered_links(
    html,
    source_url,
    *,
    host_policy=UNRESTRICTED_HOST_POLICY,
    max_links=250,
    tree=None,
):
    return _extract_discovered_links_with_policy(
        html,
        source_url,
        host_policy=host_policy,
        max_links=max_links,
        tree=tree,
    )


def _default_document_text_extractor(url, policy=UNRESTRICTED_HOST_POLICY):
    return _default_document_text_extractor_with_policy(url, policy)


async def _default_http_fetcher(*args, policy=UNRESTRICTED_HOST_POLICY, **kwargs):
    return await _default_http_fetcher_with_policy(*args, policy=policy, **kwargs)


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


def _policy(extra=None):
    policy = {
        "request_profile": {},
        "headers": {},
        "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
    }
    if extra:
        policy.update(extra)
    return policy


def _temp_cache(ttl=1, clock_start=1000.0):
    import tempfile

    from app.crawl.content_cache import ContentCache

    clock_vals = [clock_start]
    return ContentCache(tempfile.mkdtemp(), default_ttl=ttl, clock=lambda: clock_vals[0]), clock_vals


def _spawn_fetch(fn):
    server = spawn_server()
    try:
        return fn(server)
    finally:
        server.stop()


# Export every name in this module (including the underscore-prefixed app
# internals above) so ``from _testkit import *`` keeps working for the suites
# that were consolidated off the former coverage_helpers module.
# Names re-exported for the star-import suites (``from _testkit import *``).
# Kept explicit so the surface stays documented -- add a new factory here.
__all__ = [
    "Any",
    "BROWSER_HEADLESS",
    "BrowserFakeBrowser",
    "BrowserFakePage",
    "ContentCache",
    "CrawlJob",
    "CrawlResult",
    "EXTRACTION_DOCUMENT",
    "EXTRACTION_TRAFILATURA",
    "HTTP_CFFI_IMPERSONATE",
    "HTTP_DECLARED_CRAWLER",
    "FakeScheduler",
    "HttpCache",
    "HttpFetchResult",
    "FakeScheduler",
    "JobStore",
    "LLM_FORMAT_TEST_HTML",
    "MockClient",
    "MockResponse",
    "RenderOptions",
    "SCOPE_KEEP_DROP_PAGES",
    "Spill",
    "_async_return",
    "_browser_noop",
    "_collect_document_item_lines",
    "_cookie_header_for_url",
    "_decode_body",
    "_detect_pagination",
    "_default_document_text_extractor",
    "_default_http_fetcher",
    "_document_item_range",
    "execute_crawl_chain",
    "_extract_discovered_links",
    "_extract_llm_metadata",
    "_fake_browser",
    "_format_extracted_document_text",
    "_format_table",
    "_headers_for_strategy",
    "_html_diagnostics",
    "_is_document_item_marker",
    "_is_document_non_item_section",
    "_join_document_paragraph_lines",
    "_looks_like_document_heading",
    "_next_non_empty_document_line",
    "_payload_policy",
    "_policy",
    "_quality_score",
    "_should_join_document_paragraph",
    "_spawn_fetch",
    "_temp_cache",
    "assert_dns_pinning_uses_first_resolution",
    "assert_result_failed",
    "assert_result_success",
    "asyncio",
    "block_private_targets",
    "browser_available",
    "build_payload",
    "build_pinned_renderer",
    "classify_page_with_evidence",
    "crawl_homepage_async",
    "crawl_request",
    "decompress_body",
    "extract_function_body",
    "extract_content",
    "extract_js_endpoints",
    "get_env_int",
    "gzip",
    "http_strategy_policy",
    "install_to_thread_spy",
    "install_to_thread_spy",
    "invoke_browser_strategy",
    "invoke_http_strategy",
    "make_base_policy",
    "make_content_cache",
    "make_content_cache_with_entry",
    "make_crawl_payload",
    "make_engine_result",
    "make_fetch",
    "make_job",
    "make_job_store",
    "make_result",
    "UNRESTRICTED_HOST_POLICY",
    "normalize_public_http_url",
    "ok_fetcher",
    "os",
    "patch_http_fetcher_transport",
    "pathlib",
    "pinned_renderer",
    "poll_until",
    "provider_results",
    "public_url_or_none",
    "pytest",
    "raise_fetcher",
    "registrable_domain",
    "render_request",
    "run_crawl_async",
    "run_job_control_test",
    "run_scoped_crawl",
    "site_fetcher",
    "socket",
    "spawn_server",
    "to_dict",
    "urlparse",
]


def _link(url: str, source_url: str, *, normalized: str | None = None) -> dict:
    return {
        "url": url,
        "normalized_url": normalized if normalized is not None else url,
        "text": "",
        "rel": [],
        "source_url": source_url,
        "host": (normalized or url).split("//", 1)[-1].split("/", 1)[0],
        "is_same_host": True,
    }


def _canned_result(
    url: str,
    *,
    status: str = "success",
    links: list[str] | None = None,
    raw_links: list[dict] | None = None,
    classification: str | None = None,
) -> dict:
    return {
        "original_url": url,
        "normalized_url": url,
        "final_url": url,
        "status": status,
        "classification": classification or ("rendered" if status == "success" else "site_error"),
        "discovered_links": [_link(link, url) for link in links or []] + (raw_links or []),
    }


async def _execution_urls(payload_dict, hang: asyncio.Event | None):
    """Honor an execution barrier and return the payload's submitted URLs."""
    if hang is not None and not hang.is_set():
        await hang.wait()
    return payload_dict.get("urls") or [payload_dict.get("url")]


def _execution_payload(results: list[dict], submitted_count: int) -> dict:
    """Wrap engine results in the common execute-crawl response contract."""
    return {
        "results": results,
        "submitted_count": submitted_count,
        "policy_schema_version": "test",
    }


def _fake_execute_scripted(
    scripts: dict[str, list[dict]],
    *,
    attempts: dict[str, int] | None = None,
    hang: asyncio.Event | None = None,
):
    """Build a scripted execute-crawl double with per-URL attempt tracking."""
    async def _execute(payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, hang)
        results = []
        for url in urls:
            sequence = scripts.get(url)
            if sequence is None:
                entry = _canned_result(url)
            elif len(sequence) > 1:
                entry = sequence.pop(0)
            else:
                entry = sequence[0]
            if attempts is not None:
                attempts[url] = attempts.get(url, 0) + 1
            results.append(entry)
        return _execution_payload(results, len(urls))

    return _execute


def _fake_execute(results: list[dict], *, hang: asyncio.Event | None = None):
    """Build a URL-keyed execute-crawl double with optional execution blocking."""
    async def _execute(payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, hang)
        by_url = {result["original_url"]: result for result in results}
        selected = [by_url.get(url, _canned_result(url, status="failed")) for url in urls]
        return _execution_payload(selected, len(urls))

    return _execute


def _scripted_result_map(
    urls,
    *,
    links: dict[str, list[str]] | None = None,
) -> dict[str, list[dict]]:
    """Build successful scripted responses for each URL, preserving discovered links."""
    link_map = links or {}
    return {url: [_canned_result(url, links=link_map.get(url, []))] for url in urls}


def _small_link_graph_results() -> list[dict]:
    root = "http://example.test/a"
    return [
        _canned_result(
            root,
            links=[
                "http://example.test/b",
                "http://example.test/c",
                "http://example.test/d",
            ],
        ),
        _canned_result("http://example.test/b", links=["http://example.test/d"]),
        _canned_result("http://example.test/c"),
        _canned_result("http://example.test/d"),
    ]


def _make_app(job_store):
    from fastapi import FastAPI

    from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
    from app.api.jobs_router import router as jobs_router
    from app.settings import CrawlerSettings

    app = FastAPI()
    app.include_router(jobs_router)
    shutdown = asyncio.Event()
    app.dependency_overrides[get_job_store] = lambda: job_store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: shutdown
    return app


async def _wait_status(job_store, job_id: str, statuses: set[str], timeout: float = 5.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = job_store.load(job_id)
        if job is not None and job.status.value in statuses:
            return job.status.value
        await asyncio.sleep(0.01)
    raise AssertionError(f"job never reached {statuses}; last={job_store.load(job_id)}")


async def _wait_until(job_store, job_id: str, predicate, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        job = job_store.load(job_id)
        if job is not None and predicate(job):
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"predicate never satisfied; last={job_store.load(job_id)}")


def _execution_policy(policy: dict | None) -> dict:
    effective = dict(policy or {})
    limits = dict(effective.get("limits") or {})
    limits.setdefault("max_depth", 1)
    limits.setdefault("max_pages", 0)
    limits.setdefault("respect_robots", False)
    effective["limits"] = limits
    discovery = dict(effective.get("discovery") or {})
    discovery.setdefault("sitemaps", False)
    effective["discovery"] = discovery
    return effective


async def _create_api_job(client, root_url: str, execute, policy: dict | None = None):
    """Install an engine double, submit a job, and return its persisted ID."""
    from app.api import job_runner

    job_runner.install_execute_crawl(execute)
    response = await client.post(
        "/v1/jobs",
        json={"root_url": root_url, "policy": _execution_policy(policy)},
    )
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _seed_job(
    job_store,
    root_url: str,
    policy: dict | None = None,
    *,
    frontier: list[str] | None = None,
    in_flight: list[str] | None = None,
    running: bool = False,
):
    """Persist a job with explicit frontier and in-flight recovery state."""
    job = job_store.create(root_url, policy if policy is not None else {})
    if frontier is not None:
        job.frontier = list(frontier)
    if in_flight is not None:
        job.in_flight = list(in_flight)
    job_store.save(job)
    if running:
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    return job


async def _run_persisted_job(job, job_store, execute):
    """Run a pre-seeded job to a terminal state and reload its persisted record."""
    from app.api import job_runner
    from app.settings import CrawlerSettings

    job_runner.install_execute_crawl(execute)
    await job_runner._run_job(
        job.job_id,
        job_store,
        CrawlerSettings.from_mapping({}),
        asyncio.Event(),
    )
    status = await _wait_status(job_store, job.job_id, {"completed", "failed"})
    assert status in {"completed", "failed"}
    finished = job_store.load(job.job_id)
    assert finished is not None
    return finished


async def _run_scripted_persisted_job(job, job_store, result_map):
    """Run a persisted job with scripted responses and return its attempt counts."""
    attempts: dict[str, int] = {}
    finished = await _run_persisted_job(
        job,
        job_store,
        _fake_execute_scripted(result_map, attempts=attempts),
    )
    return finished, attempts


async def _run_job_to_completed(client, job_store, root_url: str, execute, policy: dict | None = None):
    """Run an API job to successful completion and return its persisted record."""
    job_id = await _create_api_job(client, root_url, execute, policy)
    status = await _wait_status(job_store, job_id, {"completed", "failed"})
    assert status == "completed"
    return job_store.load(job_id)


async def _run_scripted_job(
    client, job_store, root_url: str, sequence: list[dict]
) -> tuple[Any, dict[str, int]]:
    attempts: dict[str, int] = {}
    job = await _run_job_to_completed(
        client,
        job_store,
        root_url,
        _fake_execute_scripted({root_url: sequence}, attempts=attempts),
    )
    return job, attempts
