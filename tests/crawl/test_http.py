"""HTTP fetch strategy behavior against a real local server (no mocks)."""

import asyncio
import unittest

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from app.crawl.strategies.http import (
    _default_http_fetcher,
    _http_strategy_result,
    _looks_like_binary_body,
)
from helpers import ServerTestCase
from app.constants import HTTP_DECLARED_CRAWLER, EXTRACTION_FULL


def _run(**kwargs):
    return asyncio.run(_http_strategy_result(**kwargs))


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


class HttpStrategyTest(ServerTestCase):
    def test_http_strategy_extracts_html_markdown_and_links(self) -> None:
        result = _run(
            original_url=f"{self.base}/",
            normalized_url=f"{self.base}/",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.classification, "rendered")
        self.assertIn("Example Org", result.text or "")
        self.assertIn("Example Org", result.markdown or "")
        self.assertGreaterEqual(result.discovered_link_count, 1)
        self.assertEqual(result.next_page_url, f"{self.base}/page2")
        self.assertEqual(result.http_status, 200)

    def test_http_strategy_404_classified_not_found(self) -> None:
        result = _run(
            original_url=f"{self.base}/missing",
            normalized_url=f"{self.base}/missing",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.classification, "not_found")
        self.assertEqual(result.http_status, 404)

    def test_http_strategy_access_denied_page_blocked(self) -> None:
        result = _run(
            original_url=f"{self.base}/blocked",
            normalized_url=f"{self.base}/blocked",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        self.assertEqual(result.classification, "access_denied")
        self.assertEqual(result.status, "blocked")

    def test_http_conditional_cache_returns_not_modified_on_304(self) -> None:
        url = f"{self.base}/with_etag"
        cache = HttpCache(":memory:")
        cache.store(url, {"etag": "v1"})
        result = _run(
            original_url=url,
            normalized_url=url,
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
            cache=cache,
        )
        self.assertEqual(result.classification, "not_modified")
        self.assertEqual(result.http_status, 304)
        self.assertIsNone(result.html)

    def test_http_csv_document_via_http(self) -> None:
        result = _run(
            original_url=f"{self.base}/sample.csv",
            normalized_url=f"{self.base}/sample.csv",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        self.assertIn("Alice", result.markdown or "")
        self.assertIn("|", result.markdown or "")

    def test_http_json_document_via_http(self) -> None:
        result = _run(
            original_url=f"{self.base}/sample.json",
            normalized_url=f"{self.base}/sample.json",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        self.assertIn('"a":1', result.markdown or "")

    def test_http_pdf_table_preserved_via_http(self) -> None:
        result = _run(
            original_url=f"{self.base}/sample_table.pdf",
            normalized_url=f"{self.base}/sample_table.pdf",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher,
        )
        self.assertIn("Name", result.markdown or "")
        self.assertIn("Department", result.markdown or "")
        self.assertIn("Salary", result.markdown or "")
        self.assertIn("| Alice | Engineering | 120000 |", result.markdown or "")

    def test_looks_like_binary_body_detects_archives(self) -> None:
        self.assertTrue(_looks_like_binary_body(b"PK", "application/octet-stream", "http://x/a.zip"))
        self.assertFalse(_looks_like_binary_body(b"<html>", "text/html", "http://x/"))
        self.assertFalse(_looks_like_binary_body(b"%PDF-1.4", "application/pdf", "http://x/a.pdf"))

    def test_crawl_request_async_returns_unified_results_dict(self) -> None:
        payload = self.make_payload(
            strategies=[HTTP_DECLARED_CRAWLER],
            limits={"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
        )
        response = self.run_crawl(payload)
        self.assertEqual(response["submitted_count"], 1)
        self.assertEqual(len(response["results"]), 1)
        self.assertIn("Example Org", response["results"][0]["markdown"])


if __name__ == "__main__":
    unittest.main()
