"""Conditional-fetch contract (freshness monitoring).

Callers supply cached ETag / Last-Modified validators via ``validators``;
matching URLs are revalidated with If-None-Match / If-Modified-Since and an
unchanged page yields a not_modified result without a full body fetch.
"""

import asyncio

from app.crawl.engine import crawl_request
from app.crawl.strategies.http import _http_strategy_result
from app.models import HttpFetchResult


def _html(body: str) -> str:
    return f"<html><body>{body}</body></html>"


async def _run_http(fetcher, validators=None):
    """Drive _http_strategy_result with the standard single-page policy."""
    return await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "limits": {}, "headers": {}},
        http_fetcher=fetcher,
        caller_validators=validators,
    )


def _ok_fetcher(headers_out, body=b"hello", extra_headers=None):
    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        headers_out.update(headers)
        return HttpFetchResult(
            url=url, final_url=url, status_code=200,
            headers={"content-type": "text/html", **(extra_headers or {})},
            body=_html(body).encode(), elapsed_ms=1,
        )
    return fetcher


async def test_caller_validators_sent_as_conditional_headers():
    captured = {}
    fetcher = _ok_fetcher(captured, extra_headers={"etag": '"v1"'})
    await _run_http(fetcher, validators={
        "if_none_match": '"v1-old"',
        "if_modified_since": "Wed, 01 Jan 2025 00:00:00 GMT",
    })
    assert captured["If-None-Match"] == '"v1-old"'
    assert captured["If-Modified-Since"] == "Wed, 01 Jan 2025 00:00:00 GMT"


async def test_no_validators_means_no_conditional_headers():
    captured = {}
    await _run_http(_ok_fetcher(captured))
    assert "If-None-Match" not in captured
    assert "If-Modified-Since" not in captured


async def test_caller_validator_304_yields_not_modified():
    """A 304 against caller-owned validators is not_modified: no body exists."""
    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        return HttpFetchResult(
            url=url, final_url=url, status_code=304,
            headers={}, body=b"", elapsed_ms=1,
        )

    result = await _run_http(fetcher, validators={"if_none_match": '"v1"'})
    assert result.status == "success"
    assert result.classification == "not_modified"
    assert result.http_status == 304
    assert result.html is None and result.text is None
    # The validator is echoed so the monitor can keep polling with it.
    assert result.validators.get("etag") == '"v1"'
    assert result.diagnostics["conditional"]["If-None-Match"] == '"v1"'


async def test_success_result_surfaces_origin_validators():
    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        return HttpFetchResult(
            url=url, final_url=url, status_code=200,
            headers={"content-type": "text/html", "etag": '"abc123"',
                     "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
            body=_html("fresh").encode(), elapsed_ms=1,
        )

    result = await _run_http(fetcher)
    assert result.validators == {"etag": '"abc123"', "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT"}


async def test_engine_routes_validators_by_url(real_sleeps):
    """Batch request: validators apply only to the matching URL."""
    seen_headers: dict[str, dict] = {}

    def make_fetcher():
        async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            from urllib.parse import urlparse
            path = urlparse(url).path or "/"
            seen_headers[path] = dict(headers)
            status = 304 if (path == "/etagged" and headers.get("If-None-Match") == '"v9"') else 200
            body = _html(f"page {path}").encode() if status == 200 else b""
            return HttpFetchResult(
                url=url, final_url=url, status_code=status,
                headers={"content-type": "text/html"}, body=body, elapsed_ms=1,
            )
        return fetcher

    payload = {
        "url": "http://example.com/plain",
        "urls": None,
        "validators": {"http://example.com/etagged": {"if_none_match": '"v9"'}},
    }
    # Two URLs; only the second carries validators.
    payload2 = {
        "urls": ["http://example.com/plain", "http://example.com/etagged"],
        "validators": {"http://example.com/etagged": {"if_none_match": '"v9"'}},
    }
    response = await crawl_request(payload2, http_fetcher=make_fetcher())
    results = {r["normalized_url"]: r for r in response["results"]}
    assert len(results) == 2
    # Plain URL fetched fully (no conditional header).
    assert "If-None-Match" not in seen_headers["/plain"]
    # etagged URL revalidated conditionally -> 304 -> not_modified.
    assert seen_headers["/etagged"]["If-None-Match"] == '"v9"'
    plain_status = [r for u, r in results.items() if u.endswith("/plain")][0]
    assert plain_status["status"] == "success"


async def test_real_server_conditional_roundtrip(base_url):
    """Full roundtrip vs the real server: fetch -> validator -> revalidate 304."""
    from helpers import run_crawl_async

    url = f"{base_url}/with_etag"
    first = await run_crawl_async({"url": url})
    r1 = first["results"][0]
    assert r1["status"] == "success"
    assert r1["validators"]["etag"], r1.get("validators")
    etag = r1["validators"]["etag"]

    second = await run_crawl_async({
        "url": url,
        "validators": {url: {"if_none_match": etag}},
    })
    r2 = second["results"][0]
    assert r2["status"] == "success"
    assert r2["classification"] == "not_modified"
    assert r2["http_status"] == 304


def test_validators_conflict_with_policy_headers_rejected():
    """Conditional headers must come from validators, not raw policy headers."""
    import pytest
    from pydantic import ValidationError

    from app.api_models import CrawlRequest

    with pytest.raises(ValidationError):
        CrawlRequest(
            url="http://example.com/",
            validators={"http://example.com/": {"if_none_match": '"v1"'}},
            policy={"headers": {"If-None-Match": '"v1"'}},
        )
    # No conflict -> accepted.
    request = CrawlRequest(
        url="http://example.com/",
        validators={"http://example.com/": {"if_none_match": '"v1"'}},
        policy={"headers": {"Accept": "text/html"}},
    )
    assert request.validators["http://example.com/"].if_none_match == '"v1"'
