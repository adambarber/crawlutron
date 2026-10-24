"""Conditional-fetch contract (freshness monitoring).

Callers supply ETag / Last-Modified validators; matching URLs are revalidated
with If-None-Match / If-Modified-Since and an unchanged page is not_modified.
"""

import pytest
from pydantic import ValidationError

from app.models.crawl_request import CrawlRequest
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.orchestration.engine import crawl_request
from tests._fetcher_factories import request_fetcher, run_http_strategy, scripted_fetcher
from tests._policy_factories import http_strategy_policy


def _html(body: str) -> str:
    return f"<html><body>{body}</body></html>"


async def _run_http(fetcher, validators=None):
    """Drive http_strategy_result with the standard single-page policy."""
    return await run_http_strategy(
        url="http://example.com/",
        policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "limits": {}, "headers": {}},
        http_fetcher=fetcher,
        caller_validators=validators,
    )


def _ok_fetcher(headers_out, body=b"hello", extra_headers=None):
    """Serves ``body`` as HTML and hands back the headers it was called with."""
    spec = {
        "status": 200,
        "headers": {"content-type": "text/html", **(extra_headers or {})},
        "body": _html(body).encode(),
    }

    def respond(url, headers):
        headers_out.update(headers)
        return spec

    return request_fetcher(respond)


async def test_caller_validators_sent_as_conditional_headers():
    captured: dict[str, str] = {}
    fetcher = _ok_fetcher(captured, extra_headers={"etag": '"v1"'})
    await _run_http(
        fetcher,
        validators={
            "if_none_match": '"v1-old"',
            "if_modified_since": "Wed, 01 Jan 2025 00:00:00 GMT",
        },
    )
    assert captured["If-None-Match"] == '"v1-old"'
    assert captured["If-Modified-Since"] == "Wed, 01 Jan 2025 00:00:00 GMT"


async def test_no_validators_means_no_conditional_headers():
    captured: dict[str, str] = {}
    await _run_http(_ok_fetcher(captured))
    assert "If-None-Match" not in captured
    assert "If-Modified-Since" not in captured


async def test_caller_validator_304_yields_not_modified():
    """A 304 against caller-owned validators is not_modified: no body exists."""

    fetcher = scripted_fetcher(status=304, headers={}, body=b"")

    result = await _run_http(fetcher, validators={"if_none_match": '"v1"'})
    assert result.status == "success"
    assert result.classification == "not_modified"
    assert result.http_status == 304
    assert result.html is None and result.text is None
    # The validator is echoed so the monitor can keep polling with it.
    assert result.validators.get("etag") == '"v1"'
    assert result.diagnostics["conditional"]["If-None-Match"] == '"v1"'


async def test_success_result_surfaces_origin_validators():
    fetcher = scripted_fetcher(
        status=200,
        headers={
            "content-type": "text/html",
            "etag": '"abc123"',
            "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT",
        },
        body=_html("fresh").encode(),
    )

    result = await _run_http(fetcher)
    assert result.validators == {"etag": '"abc123"', "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT"}


async def test_engine_routes_validators_by_url():
    """Batch request: validators apply only to the matching URL."""
    seen_headers: dict[str, dict] = {}

    def make_fetcher():
        def respond(url, headers):
            path = url.path or "/"
            seen_headers[path] = dict(headers)
            revalidated = path == "/etagged" and headers.get("If-None-Match") == '"v9"'
            if revalidated:
                return (304, b"", "text/html")
            return (200, _html(f"page {path}"), "text/html")

        return request_fetcher(respond)

    # Two URLs; only the second carries validators.
    payload2 = {
        "urls": ["http://example.com/plain", "http://example.com/etagged"],
        "validators": {"http://example.com/etagged": {"if_none_match": '"v9"'}},
    }
    response = await crawl_request(payload2, http_fetcher=make_fetcher())
    results = {r.normalized_url: r for r in response["results"]}
    assert len(results) == 2
    # Plain URL fetched fully (no conditional header).
    assert "If-None-Match" not in seen_headers["/plain"]
    # Etagged URL revalidated conditionally -> 304 -> not_modified.
    assert seen_headers["/etagged"]["If-None-Match"] == '"v9"'
    plain = [r for u, r in results.items() if u.raw.endswith("/plain")][0]
    assert plain.status == "success"


async def test_real_server_conditional_roundtrip(base_url):
    """Full roundtrip vs the real server: fetch -> validator -> revalidate 304."""
    from tests._fetcher_factories import run_crawl_async

    url = f"{base_url}/with_etag"
    first = await run_crawl_async({"url": url})
    r1 = first["results"][0]
    assert r1.status == "success"
    assert r1.validators["etag"], r1.validators
    etag = r1.validators["etag"]

    second = await run_crawl_async(
        {
            "url": url,
            "validators": {url: {"if_none_match": etag}},
        }
    )
    r2 = second["results"][0]
    assert r2.status == "success"
    assert r2.classification == "not_modified"
    assert r2.http_status == 304


def test_validators_conflict_with_policy_headers_rejected():
    """Conditional headers must come from validators, not raw policy headers."""
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


def test_conditional_headers_rejected_without_validators():
    """Raw If-* in policy.headers is rejected even with no validators.

    Without validators the raw headers flow into the fetch, and an origin 304
    yields a bodyless result -- caller-controlled content suppression.
    """
    with pytest.raises(ValidationError):
        CrawlRequest(
            url="http://example.com/",
            policy={"headers": {"If-None-Match": '"v1"'}},
        )
    with pytest.raises(ValidationError):
        CrawlRequest(
            url="http://example.com/",
            policy={"headers": {"if-modified-since": "Wed, 01 Jan 2024 00:00:00 GMT"}},
        )
    # Ordinary headers are still accepted.
    request = CrawlRequest(
        url="http://example.com/",
        policy={"headers": {"Accept": "text/html"}},
    )
    assert request.policy.headers == {"Accept": "text/html"}


async def test_malformed_gzip_is_rejected_by_strategy():
    import gzip

    from app.transport.decompression import DecompressionTooLargeError, decompress_body

    compressed = bytearray(gzip.compress(_html("real content").encode()))
    for i in range(2, 12):
        compressed[i] ^= 0xFF

    fetcher = scripted_fetcher(
        status=200,
        headers={"content-type": "text/html", "content-encoding": "gzip"},
        body=bytes(compressed),
    )

    result = await _run_http(fetcher)
    assert result.status == "failed"
    # The body is the server's fault, not the caller's.
    assert result.error == "http_error"
    assert result.html is None
    assert result.text is None

    with pytest.raises(DecompressionTooLargeError):
        decompress_body(gzip.compress(b"x" * 4096), "gzip", max_output_bytes=8)


class TestNotModifiedResponse:
    async def test_http_strategy_not_modified(self):
        from app.transport.cache import HttpCache

        not_mod_fetcher = scripted_fetcher(
            status=304, headers={"etag": "v1", "content-type": "text/html"}, body=b"not modified"
        )

        cache = HttpCache()
        # The cache is value-keyed: seed it with the admitted value the strategy will read under, or the
        # conditional GET silently misses.
        cache.store(NormalizedUrl("http://example.com/", policy=HostPolicy.unrestricted()), {"etag": "v1"})
        result = await run_http_strategy(
            url="http://example.com/",
            policy=http_strategy_policy(),
            http_fetcher=not_mod_fetcher,
            cache=cache,
        )
        assert result.classification == "not_modified"
