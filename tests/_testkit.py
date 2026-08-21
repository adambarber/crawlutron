"""Shared test helpers for the browser-crawler test suite.

Centralizes the small factories/fixtures that were previously copy-pasted
across several test modules (``site_fetcher``, ``block_private_targets``,
``make_job``, ``make_result``) so the suite stays DRY and jscpd reports no
duplication.
"""
from typing import Any
from urllib.parse import urlparse

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
