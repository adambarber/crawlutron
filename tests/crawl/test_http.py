"""HTTP fetch strategy behavior against a real local server (no mocks)."""

import pytest

from app.crawl.cache import HttpCache
from app.models import HttpFetchResult
from app.crawl.engine import crawl_request
from app.crawl.strategies.http import (
    _default_http_fetcher,
    _http_strategy_result,
    _looks_like_binary_body,
)
from app.constants import HTTP_DECLARED_CRAWLER, EXTRACTION_FULL


async def _run(**kwargs):
    return await _http_strategy_result(**kwargs)


def http_policy(**overrides):
    policy = {
        "strategies": [HTTP_DECLARED_CRAWLER],
        "extraction": EXTRACTION_FULL,
        "scope_regex": None,
        "limits": {"timeout_seconds": 10, "max_retries": 0, "challenge_wait_seconds": 15, "max_discovered_links": 50},
        "headers": {},
        "request_profile": {"user_agent_family": "generic_crawler", "cookie_jar": "none"},
        "browser_profile": {},
        "discovery": {},
    }
    policy.update(overrides)
    return policy


class TestHttpStrategy:
    async def test_html_diagnostics_skipped_without_discovery(self):
        # #2: _html_diagnostics (frames/forms via lxml parse) must be skipped
        # when discovery is not requested, to avoid a redundant HTML parse on
        # every page of a content-only crawl.
        html = "<html><body><iframe src='/x'></iframe></body></html>"

        async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            return HttpFetchResult(
                url=url, final_url=url, status_code=200,
                headers={"content-type": "text/html"}, body=html.encode("utf-8"), elapsed_ms=1,
            )

        kwargs = dict(
            original_url="http://x.test/", normalized_url="http://x.test/",
            strategy=HTTP_DECLARED_CRAWLER, http_fetcher=fetcher,
            cache=HttpCache(), content_cache=None,
        )
        # discovery omitted -> no frames in diagnostics
        r1 = await _run(policy=http_policy(discovery=None), **kwargs)
        assert "frames" not in r1.diagnostics
        # discovery requested -> frames computed
        r2 = await _run(policy=http_policy(discovery={"sitemaps": True}), **kwargs)
        assert "frames" in r2.diagnostics

    async def test_http_strategy_extracts_html_markdown_and_links(self, base_url):
        result = await _run(
            original_url=f"{base_url}/",
            normalized_url=f"{base_url}/",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        assert result.status == "success"
        assert result.classification == "rendered"
        assert "Example Org" in (result.text or "")
        assert "Example Org" in (result.markdown or "")
        assert result.discovered_link_count >= 1
        assert result.next_page_url == f"{base_url}/page2"
        assert result.http_status == 200

    async def test_http_strategy_404_classified_not_found(self, base_url):
        result = await _run(
            original_url=f"{base_url}/missing",
            normalized_url=f"{base_url}/missing",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        assert result.status == "failed"
        assert result.classification == "not_found"
        assert result.http_status == 404

    async def test_http_strategy_access_denied_page_blocked(self, base_url):
        result = await _run(
            original_url=f"{base_url}/blocked",
            normalized_url=f"{base_url}/blocked",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        assert result.classification == "access_denied"
        assert result.status == "blocked"

    async def test_http_conditional_cache_returns_not_modified_on_304(self, base_url):
        url = f"{base_url}/with_etag"
        cache = HttpCache(":memory:")
        cache.store(url, {"etag": "v1"})
        result = await _run(
            original_url=url,
            normalized_url=url,
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
            cache=cache,
        )
        assert result.classification == "not_modified"
        assert result.http_status == 304
        assert result.html is None

    async def test_http_csv_document_via_http(self, base_url):
        result = await _run(
            original_url=f"{base_url}/sample.csv",
            normalized_url=f"{base_url}/sample.csv",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        assert "Alice" in (result.markdown or "")
        assert "|" in (result.markdown or "")

    async def test_http_json_document_via_http(self, base_url):
        result = await _run(
            original_url=f"{base_url}/sample.json",
            normalized_url=f"{base_url}/sample.json",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        assert '"a":1' in (result.markdown or "")

    async def test_http_pdf_table_preserved_via_http(self, base_url):
        result = await _run(
            original_url=f"{base_url}/sample_table.pdf",
            normalized_url=f"{base_url}/sample_table.pdf",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        assert "Name" in (result.markdown or "")
        assert "Department" in (result.markdown or "")
        assert "Salary" in (result.markdown or "")
        assert "| Alice | Engineering | 120000 |" in (result.markdown or "")

    def test_looks_like_binary_body_detects_archives(self):
        assert _looks_like_binary_body(b"PK\x03\x04", "application/octet-stream", "http://x/a.zip")
        assert not _looks_like_binary_body(b"<html>", "text/html", "http://x/")
        assert not _looks_like_binary_body(b"%PDF-1.4", "application/pdf", "http://x/a.pdf")

    async def test_crawl_request_async_returns_unified_results_dict(self, base_url):
        from helpers import crawl_homepage_async
        response = await crawl_homepage_async(base_url)
        assert response["submitted_count"] == 1
        assert len(response["results"]) == 1
        assert "Example Org" in response["results"][0]["markdown"]
