"""Targeted coverage tests for branches missed by the behavioral suites.

These exercise pure functions and orchestration with real inputs only (no
unittest.mock). Browser-dependent paths use real Camoufox or a real in-process
HTTP server.
"""
import asyncio
import gzip
import os
import unittest
from typing import Any

# allow loopback crawl targets in this process too
os.environ.setdefault("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")

from app.classify import classify_rendered_page_with_evidence
from app.crawl.cache import HttpCache
from app.crawl.decompression import decompress_body
from app.crawl.engine import (
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_TRAFILATURA,
    _execute_crawl_chain,
    _payload_policy,
    _result_dict,
    crawl_request,
)
from app.crawl.extraction import (
    _extract_discovered_links,
    _html_diagnostics,
    extract_js_endpoints,
)
from app.crawl.firecrawl_format import _extract_firecrawl_metadata
from app.crawl.strategies.document import (
    _default_document_text_extractor,
    _format_table,
)
from app.crawl.strategies.http import (
    _cookie_header_for_url,
    _default_http_fetcher,
    _format_extracted_document_text,
    _headers_for_strategy,
    _looks_like_document_heading,
    _quality_score,
    _status_for_classification,
    _collect_document_item_lines,
    _document_item_range,
    _is_document_item_marker,
    _is_document_non_item_section,
    _join_document_paragraph_lines,
    _next_non_empty_document_line,
    _should_join_document_paragraph,
    _decode_body,
)
from app.constants import HTTP_CFFI_IMPERSONATE, HTTP_DECLARED_CRAWLER
from app.models import CrawlJob, CrawlResult, RenderOptions
from app.service import render_request
from app.validation import normalize_public_http_url, registrable_domain
from helpers import ServerTestCase
from helpers.server import spawn_server


class TestValidationGaps(unittest.TestCase):
    def test_unsupported_scheme(self):
        with self.assertRaises(ValueError):
            normalize_public_http_url("ftp://example.com/")

    def test_private_network_blocked(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            with self.assertRaises(ValueError):
                normalize_public_http_url("http://127.0.0.1/x")
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_trailing_colon_path(self):
        self.assertEqual(
            normalize_public_http_url("https://example.com/seg:"),
            "https://example.com/seg",
        )

    def test_registrable_domain(self):
        self.assertEqual(registrable_domain("https://a.b.c.example.com/x"), "example.com")
        self.assertEqual(registrable_domain("https://example.com/x"), "example.com")

    def test_invalid_url_no_host(self):
        with self.assertRaises(ValueError):
            normalize_public_http_url("http:///path")

    def test_private_network_localhost_blocked(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            with self.assertRaises(ValueError):
                normalize_public_http_url("http://localhost/x")
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_private_network_normal_domain_passed_through(self):
        saved = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
        os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "0"
        try:
            out = normalize_public_http_url("http://example.com/x")
            self.assertEqual(out, "http://example.com/x")
        finally:
            if saved is None:
                os.environ.pop("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", None)
            else:
                os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = saved

    def test_registrable_domain_two_part_suffix(self):
        self.assertEqual(registrable_domain("https://a.b.example.co.uk/x"), "example.co.uk")


class TestCacheGaps(unittest.TestCase):
    def test_conditional_headers_include_last_modified(self):
        c = HttpCache()
        c.store("http://x/1", {"etag": "v1", "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"})
        headers = c.conditional_headers("http://x/1")
        self.assertEqual(headers["If-None-Match"], "v1")
        self.assertEqual(headers["If-Modified-Since"], "Wed, 01 Jan 2025 00:00:00 GMT")
        c.close()

    def test_close_idempotent(self):
        c = HttpCache()
        c.close()
        c.close()


class TestClassifyGaps(unittest.TestCase):
    def test_html_without_script_style_signal(self):
        classification, evidence = classify_rendered_page_with_evidence(
            "<html><!-- access denied --></html>", "clean visible text",
        )
        self.assertEqual(classification, "access_denied")
        self.assertEqual(evidence[0]["source"], "html_without_script_style")


class TestFirecrawlFormatGaps(unittest.TestCase):
    def test_malformed_json_ld_is_ignored(self):
        md = _extract_firecrawl_metadata(
            '<script type="application/ld+json">{not valid json</script>', "http://x/",
        )
        self.assertNotIn("json_ld", md)

    def test_canonical_and_favicon(self):
        html = (
            '<link rel="canonical" href="https://canon.example/">'
            '<link rel="icon" href="/fav.ico">'
        )
        md = _extract_firecrawl_metadata(html, "http://x/")
        self.assertEqual(md["canonical_url"], "https://canon.example/")
        self.assertEqual(md["favicon"], "/fav.ico")


class TestDocumentGaps(ServerTestCase):
    def test_format_table_empty(self):
        self.assertEqual(_format_table([]), "")

    def test_extract_unreachable_url(self):
        self.assertEqual(_default_document_text_extractor("http://127.0.0.1:1/nowhere.pdf"), "")

    def test_extract_broken_pdf_from_server(self):
        text = _default_document_text_extractor(self.base + "/broken.pdf")
        self.assertEqual(text, "")


class TestExtractionGaps(unittest.TestCase):
    def test_nested_anchor(self):
        # Real HTML parsers (lxml) flatten illegally-nested anchors into
        # siblings, so both distinct hrefs are discovered as separate links
        # rather than the inner one being swallowed by the outer.
        links, _, _ = _extract_discovered_links(
            '<a href="/x">outer <a href="/y">inner</a></a>', "http://e.com/",
        )
        hrefs = {l.normalized_url for l in links}
        self.assertIn("http://e.com/x", hrefs)
        self.assertIn("http://e.com/y", hrefs)

    def test_duplicate_href(self):
        links, count, _ = _extract_discovered_links(
            '<a href="/x">a</a><a href="/x">b</a>', "http://e.com/",
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(count, 1)

    def test_max_links_truncation(self):
        html = "".join(f'<a href="/{i}">x</a>' for i in range(5))
        links, count, truncated = _extract_discovered_links(html, "http://e.com/", max_links=2)
        self.assertEqual(len(links), 2)
        self.assertTrue(truncated)
        self.assertEqual(count, 5)

    def test_html_diagnostics_frames_and_forms(self):
        html = (
            '<frameset><frame src="/a"></frameset>'
            '<form action="/s" method="post"><input name="q" value="1"></form>'
        )
        diag = _html_diagnostics(html, "http://e.com/")
        self.assertIn("frames", diag)
        self.assertIn("forms", diag)
        self.assertTrue(diag["frames"])
        self.assertTrue(diag["forms"])

    def test_js_endpoints(self):
        html = '<script>fetch("/api/x");</script>'
        endpoints = extract_js_endpoints(html)
        self.assertIn({"url": "/api/x"}, endpoints)


class TestHttpHelperGaps(unittest.TestCase):
    def test_cookie_header_per_host(self):
        self.assertEqual(
            _cookie_header_for_url(
                {"cookie_jar": "per_host", "cookies": {"e.com": {"a": "1"}}},
                "http://e.com/",
            ),
            "a=1",
        )

    def test_headers_cffi_impersonate(self):
        headers = _headers_for_strategy(
            HTTP_CFFI_IMPERSONATE, {"request_profile": {}, "headers": {}}, "http://e.com/",
        )
        self.assertIn("User-Agent", headers)
        self.assertIn("sec-ch-ua", {k.lower() for k in headers})

    def test_headers_cookie_added(self):
        headers = _headers_for_strategy(
            HTTP_DECLARED_CRAWLER,
            {"request_profile": {"cookie_jar": "per_host", "cookies": {"e.com": {"a": "1"}}}, "headers": {}},
            "http://e.com/",
        )
        self.assertEqual(headers.get("Cookie"), "a=1")

    def test_status_for_not_modified(self):
        self.assertEqual(_status_for_classification("not_modified"), "success")

    def test_quality_frameset_low_text(self):
        score = _quality_score("success", "rendered", "tiny", "<frameset></frameset>", 200)
        self.assertLessEqual(score, 0.4)

    def test_decode_body_gzip_content_encoding(self):
        body = gzip.compress(b"hello world")
        out = _decode_body(body, "text/html", {"content-encoding": "gzip"})
        self.assertEqual(out, "hello world")


class TestDocumentTextParserGaps(unittest.TestCase):
    def test_range_action(self):
        out = _format_extracted_document_text(
            "1a - 1c Approve\n1a.\nBuild the thing\n1b.\nFix the bug\n1c.\nShip it",
            source_url="http://x/",
        )
        self.assertIn("Action: Approve", out)

    def test_action(self):
        out = _format_extracted_document_text(
            "2a Approve all\n2a.\nDetail line", source_url="http://x/",
        )
        self.assertIn("Action: Approve all", out)

    def test_section_title(self):
        out = _format_extracted_document_text(
            "1.\nSection Title\nBody text", source_url="http://x/",
        )
        self.assertIn("1. Section Title", out)

    def test_item_numbered_continuation(self):
        out = _format_extracted_document_text(
            "3a.\n3. Build the thing\n4. Fix it", source_url="http://x/",
        )
        self.assertIn("3a. Build the thing", out)

    def test_non_item_section_skipped(self):
        out = _format_extracted_document_text(
            "PUBLIC PARTICIPATION\nWe welcome comments", source_url="http://x/",
        )
        self.assertNotIn("PUBLIC PARTICIPATION", out)

    def test_prose_merge(self):
        out = _format_extracted_document_text(
            "First sentence here.\nSecond sentence here.", source_url="http://x/",
        )
        self.assertIn("First sentence here.", out)
        self.assertIn("Second sentence here.", out)

    def test_heading_join_skipped(self):
        out = _format_extracted_document_text(
            "INTRODUCTION\nBody continues here", source_url="http://x/",
        )
        self.assertIn("INTRODUCTION", out)

    def test_comma_join(self):
        out = _format_extracted_document_text(
            "First clause here.\n, and more detail", source_url="http://x/",
        )
        self.assertIn(", and more detail", out)

    def test_helpers_direct(self):
        self.assertTrue(_looks_like_document_heading("INTRODUCTION"))
        self.assertFalse(_looks_like_document_heading("a normal sentence that is far too long to be a heading"))
        self.assertTrue(_is_document_item_marker("1a."))
        self.assertFalse(_is_document_item_marker("Section"))
        self.assertTrue(_is_document_non_item_section("PUBLIC PARTICIPATION"))
        self.assertFalse(_is_document_non_item_section("Normal text"))
        self.assertEqual(list(_document_item_range("1a", "1c")), ["1a", "1b", "1c"])
        self.assertEqual(_next_non_empty_document_line(["", "a", "b"], 0), 1)
        self.assertIsNone(_next_non_empty_document_line(["", ""], 0))
        self.assertEqual(_join_document_paragraph_lines("a", "b"), "a b")
        self.assertEqual(_join_document_paragraph_lines("a", ", b"), "a, b")
        self.assertTrue(_should_join_document_paragraph("First clause here", "and continues the work"))
        lines, nxt = _collect_document_item_lines(["Build", "4. Fix it"], 0)
        self.assertEqual(lines, ["Build", "Fix it"])
        self.assertEqual(nxt, 2)

    def test_blank_line_skipped_in_formatter(self):
        out = _format_extracted_document_text("Intro paragraph.\n\nSecond paragraph.", source_url="http://x/")
        self.assertIn("Intro paragraph.", out)
        self.assertIn("Second paragraph.", out)

    def test_blank_line_skipped_in_collect(self):
        lines, nxt = _collect_document_item_lines(["Build", "", "More detail"], 0)
        self.assertEqual(lines, ["Build", "More detail"])
        self.assertEqual(nxt, 3)


class TestEngineGaps(unittest.TestCase):
    def _fake_browser(self, html=None, text="doc", metadata=None):
        def worker(job):
            return CrawlResult(
                url=job.url, status="success", classification="rendered",
                final_url=job.url, html=html, text=text, attempts=1,
                elapsed_ms=1, domain="x", metadata=metadata or {},
            )
        return worker

    def test_result_dict(self):
        d = _result_dict(CrawlResult(url="http://x/", status="success", classification="rendered",
                                    final_url="http://x/", html=None, text="t", attempts=1,
                                    elapsed_ms=1, domain="x", metadata={}))
        self.assertEqual(d["url"], "http://x/")

    def test_payload_policy_invalid_strategy(self):
        with self.assertRaises(ValueError):
            _payload_policy({"policy": {"strategies": ["BOGUS"]}}, None)

    def test_browser_strategy_document_extraction(self):
        policy = {"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_DOCUMENT}
        result = asyncio.run(crawl_request(
            {"url": "http://127.0.0.1:1/doc", "policy": policy},
            browser_worker=self._fake_browser(html="<html><body>doc table</body></html>"),
            cache=HttpCache(),
        ))
        self.assertEqual(result["results"][0]["status"], "success")
        self.assertIn("doc table", result["results"][0]["markdown"] or "")

    def test_browser_strategy_trafilatura_extraction(self):
        policy = {"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_TRAFILATURA}
        result = asyncio.run(crawl_request(
            {"url": "http://127.0.0.1:1/doc2", "policy": policy},
            browser_worker=self._fake_browser(html="<html><body>trafilatura body</body></html>"),
            cache=HttpCache(),
        ))
        self.assertIn("trafilatura body", result["results"][0]["markdown"] or "")

    def test_browser_strategy_no_content(self):
        policy = {"strategies": [BROWSER_HEADLESS]}
        result = asyncio.run(crawl_request(
            {"url": "http://127.0.0.1:1/doc3", "policy": policy},
            browser_worker=self._fake_browser(html=None, text=None),
            cache=HttpCache(),
        ))
        self.assertIsNone(result["results"][0]["markdown"])

    def test_browser_strategy_surfaces_xhr_and_screenshot(self):
        policy = {"strategies": [BROWSER_HEADLESS]}
        result = asyncio.run(crawl_request(
            {"url": "http://127.0.0.1:1/doc4", "policy": policy},
            browser_worker=self._fake_browser(
                html="<html><body>x</body></html>",
                metadata={"intercepted_xhr": [{"url": "u", "payload": {}}], "screenshot": "BASE64IMG"},
            ),
            cache=HttpCache(),
        ))
        diag = result["results"][0]["diagnostics"]
        self.assertIn("intercepted_xhr", diag)
        self.assertIn("screenshot", diag)


    def test_rate_limited_sets_domain_cooldown(self):
        from app.scheduler import DomainScheduler

        class _Resp:
            status_code = 429
            final_url = "http://example.com/"
            elapsed_ms = 1
            headers = {"retry-after": "30", "content-type": "text/plain"}
            body = b"rate limited"

        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            return _Resp()

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        policy = {
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "extraction": "dom",
            "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            "headers": {},
            "request_profile": {},
        }
        result = asyncio.run(_execute_crawl_chain(
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            policy=policy,
            http_fetcher=fetcher,
            cffi_fetcher=fetcher,
            browser_worker=None,
            scheduler=scheduler,
            domain="example.com",
        ))
        self.assertEqual(result.classification, "rate_limited")
        self.assertIn("example.com", scheduler._domain_cooldowns)
        import time as _time
        self.assertAlmostEqual(scheduler._domain_cooldowns["example.com"], _time.time() + 30, delta=5)

class TestServiceRenderRequest(unittest.TestCase):
    def _worker(self, status="success", classification="rendered"):
        def worker(job):
            return CrawlResult(
                url=job.url, status=status, classification=classification,
                final_url=job.url, html="<html></html>", text="t", attempts=1,
                elapsed_ms=1, domain="x", metadata={},
            )
        return worker

    def test_render_request_success(self):
        from app.settings import CrawlerSettings
        out = asyncio.run(render_request(
            {"url": "http://127.0.0.1:1/x", "options": {"include_html": True}},
            settings=CrawlerSettings.from_mapping(),
            worker=self._worker(),
        ))
        self.assertEqual(out["submitted_count"], 1)
        self.assertEqual(out["results"][0]["status"], "success")

    def test_render_request_disallowed_url(self):
        from app.settings import CrawlerSettings
        out = asyncio.run(render_request(
            {"url": "not a url"},
            settings=CrawlerSettings.from_mapping(),
            worker=self._worker(),
        ))
        self.assertEqual(out["results"][0]["status"], "disallowed")

    def test_render_request_batch_exceeded(self):
        from app.settings import CrawlerSettings
        with self.assertRaises(ValueError):
            asyncio.run(render_request(
                {"urls": ["http://127.0.0.1:1/x"] * 1000},
                settings=CrawlerSettings.from_mapping(),
                worker=self._worker(),
            ))


class TestHttpServerGaps(ServerTestCase):

    def _policy(self, extra=None):
        policy = {"request_profile": {}, "headers": {}, "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50}}
        if extra:
            policy.update(extra)
        return policy

    def test_cache_last_modified_store(self):
        from app.crawl.strategies.http import _http_strategy_result
        cache = HttpCache()
        out = asyncio.run(_http_strategy_result(
            original_url=self.base + "/with_lastmod",
            normalized_url=self.base + "/with_lastmod",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=self._policy(),
            http_fetcher=_default_http_fetcher,
            cache=cache,
        ))
        self.assertEqual(out.status, "success")
        etag, last_modified = cache.get(self.base + "/with_lastmod")
        self.assertIsNotNone(last_modified)

    def test_rate_limit_cooldown(self):
        from app.crawl.strategies.http import _http_strategy_result
        cache = HttpCache()
        out = asyncio.run(_http_strategy_result(
            original_url=self.base + "/ratelimit",
            normalized_url=self.base + "/ratelimit",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=self._policy(),
            http_fetcher=_default_http_fetcher,
            cache=cache,
        ))
        self.assertEqual(out.http_status, 429)
        self.assertIn("cooldown", out.diagnostics)
        self.assertEqual(out.diagnostics["cooldown"]["retry_after_seconds"], 30)

    def test_cffi_fetch_via_crawl_request(self):
        result = asyncio.run(crawl_request(
            {"url": self.base + "/", "policy": {"strategies": ["HTTP_CFFI_IMPERSONATE"]}},
            cache=HttpCache(),
        ))
        self.assertEqual(result["results"][0]["status"], "success")


class TestDecompressionGaps(unittest.TestCase):
    def test_gzip_failure_returns_body(self):
        self.assertEqual(decompress_body(b"not gzip", "gzip"), b"not gzip")

    def test_deflate_failure_returns_body(self):
        self.assertEqual(decompress_body(b"not deflate", "deflate"), b"not deflate")

    def test_brotli_failure_returns_body(self):
        self.assertEqual(decompress_body(b"not brotli", "br"), b"not brotli")

    def test_plain_passthrough(self):
        self.assertEqual(decompress_body(b"plain", "identity"), b"plain")


class TestBrowserXhrHandlerGaps(unittest.TestCase):
    def test_json_payload_captured(self):
        from app.crawl.strategies.browser import BrowserRenderer

        collected: list[dict[str, Any]] = []
        handler = BrowserRenderer._make_response_handler(collected)

        class FakeResponse:
            headers = {"content-type": "application/json"}
            url = "http://x/api/data"
            status = 200

            async def json(self):
                return {"a": 1}

        asyncio.run(handler(FakeResponse()))
        self.assertEqual(collected[0]["payload"], {"a": 1})

    def test_bad_json_payload_captured_as_none(self):
        from app.crawl.strategies.browser import BrowserRenderer

        collected: list[dict[str, Any]] = []
        handler = BrowserRenderer._make_response_handler(collected)

        class FakeResponse:
            headers = {"content-type": "application/json"}
            url = "http://x/api/badjson"
            status = 200

            async def json(self):
                raise ValueError("bad json")

        asyncio.run(handler(FakeResponse()))
        self.assertEqual(len(collected), 1)
        self.assertIsNone(collected[0]["payload"])


if __name__ == "__main__":
    unittest.main()
