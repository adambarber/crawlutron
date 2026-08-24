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
                url=url, final_url=url, status_code=404,
                headers={"content-type": "text/html"}, body=b"<html></html>", elapsed_ms=1,
            )
        status, body, ctype = entry
        if isinstance(body, str):
            body = body.encode("utf-8")
        return HttpFetchResult(
            url=url, final_url=url, status_code=status,
            headers={"content-type": ctype}, body=body, elapsed_ms=1,
        )
    return fetcher


def make_job(index: int, url: str):
    from app.models import CrawlJob
    return CrawlJob(index=index, url=url, domain=url.split("/")[2], options=None)


def make_result(url: str, domain: str):
    from app.models import CrawlStrategyAttempt, EngineCrawlResult
    return EngineCrawlResult(
        original_url=url, normalized_url=url, final_url=url,
        status="success", classification="rendered",
        selected_strategy="HTTP_DECLARED_CRAWLER",
        attempted_strategies=[CrawlStrategyAttempt(
            strategy="HTTP_DECLARED_CRAWLER", status="success",
            classification="rendered", http_status=200,
            content_type="text/html", elapsed_ms=0, error=None)],
        http_status=200, headers={}, content_type="text/html",
        html="<html></html>", text="ok", markdown="ok",
        extraction_method="http_html", quality_score=1.0,
        classification_evidence=[], diagnostics={},
        discovered_link_count=0, discovered_links_truncated=False,
        discovered_links=[], error=None, next_page_url=None,
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

def make_fetch(status=200, body=b"<html><body>hello</body></html>", ctype="text/html; charset=utf-8",
               final_url="http://example.com/", url="http://example.com/"):
    return HttpFetchResult(url=url, final_url=final_url, status_code=status,
                           headers={"content-type": ctype}, body=body, elapsed_ms=1)


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
        BrowserRenderer, "_extract_with_challenge_wait",
        lambda self, page, options: (_async_return(("<html>hi</html>", "hi", "ok", False))),
    )
    monkeypatch.setattr(BrowserRenderer, "_looks_like_pdf", lambda self, url: False)
    monkeypatch.setattr(BrowserRenderer, "_status_for_classification", lambda self, c: "success")
    return renderer
