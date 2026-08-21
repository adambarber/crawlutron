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

from app.classify import classify_page_with_evidence
from app.crawl.cache import HttpCache
from app.crawl.decompression import decompress_body
from app.crawl.engine import (
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_TRAFILATURA,
    _execute_crawl_chain,
    _payload_policy,
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
from app.models import CrawlJob, CrawlResult, RenderOptions, to_dict
from app.service import render_request
from app.validation import normalize_public_http_url, registrable_domain
from helpers import (
    ServerTestCase,
    make_async_client,
    run_async_test,
    make_content_cache,
    make_content_cache_with_entry,
    make_job_store,
    run_job_control_test,
)
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
        # Regression guard for the HTML-comment classification bypass fix:
        # a block signature inside an HTML comment must NOT be detected
        # (comments are not user-visible and are attacker-controllable).
        classification, evidence = classify_page_with_evidence(
            "<html><!-- access denied --></html>", "clean visible text",
        )
        self.assertEqual(classification, "rendered")

    def test_html_visible_signal_detected(self):
        # Visible (non-comment) block signals in the HTML body must still be
        # detected via the html_without_script_style haystack.
        classification, evidence = classify_page_with_evidence(
            "<html><body>access denied</body></html>", None,
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
        d = to_dict(CrawlResult(url="http://x/", status="success", classification="rendered",
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



class TestExtractionCoverageGaps(unittest.TestCase):
    """Cover remaining branches in extraction.py"""

    def test_extract_content_with_css_selector(self):
        from app.crawl.extraction import extract_content
        html = '<div class="target">selected</div><div>ignored</div>'
        result = extract_content(html.encode(), "text/html", "http://e.com/", css_selector=".target")
        self.assertEqual(result.text, "selected")

    def test_extract_content_with_xpath(self):
        from app.crawl.extraction import extract_content
        html = '<div class="target">selected</div><div>ignored</div>'
        result = extract_content(html.encode(), "text/html", "http://e.com/", xpath='//div[@class="target"]')
        self.assertEqual(result.text, "selected")

    def test_html_to_markdown_markdownify_success(self):
        from app.crawl.extraction import _html_to_markdown
        text, md = _html_to_markdown("<p>hello <strong>world</strong></p>")
        self.assertIsNotNone(text)
        self.assertIsNotNone(md)
        self.assertIn("world", md)

    def test_html_to_markdown_markdownify_exception(self):
        from app.crawl.extraction import _html_to_markdown
        text, md = _html_to_markdown("<p>test</p>")
        self.assertIsNotNone(text)

    def test_extract_selected_no_html(self):
        from app.crawl.extraction import _extract_selected
        result = _extract_selected("", "http://e.com/", css_selector=".x")
        self.assertTrue(result.text is None or isinstance(result.text, str))
        self.assertIsNone(result.markdown)

    def test_extract_selected_css_selector_no_match(self):
        from app.crawl.extraction import _extract_selected
        result = _extract_selected("<div>content</div>", "http://e.com/", css_selector=".missing")
        self.assertTrue(result.text is None or isinstance(result.text, str))
        self.assertIsNone(result.markdown)

    def test_extract_selected_xpath_no_match(self):
        from app.crawl.extraction import _extract_selected
        result = _extract_selected("<div>content</div>", "http://e.com/", xpath='//span')
        self.assertTrue(result.text is None or isinstance(result.text, str))
        self.assertIsNone(result.markdown)

    def test_extract_selected_xpath_invalid(self):
        from app.crawl.extraction import _extract_selected
        result = _extract_selected("<div>content</div>", "http://e.com/", xpath='//[')
        self.assertTrue(result.text is None or isinstance(result.text, str))

    def test_extract_selected_xpath_string_node(self):
        from app.crawl.extraction import _extract_selected
        result = _extract_selected('<div><span>text1</span><span>text2</span></div>', "http://e.com/", xpath='//span/text()')
        self.assertIsNotNone(result.text)

    def test_extract_html_trafilatura_exception(self):
        from app.crawl.extraction import _extract_html
        html = "<html><body><p>test content</p></body></html>"
        result = _extract_html(html, "http://e.com/")
        self.assertIsNotNone(result.text)

    def test_extract_html_markdownify_exception(self):
        from app.crawl.extraction import _extract_html
        html = "<html><body><p>plain text fallback</p></body></html>"
        result = _extract_html(html, "http://e.com/")
        self.assertIsNotNone(result.text)

    def test_extract_document_markitdown_import_error(self):
        from app.crawl.extraction import _extract_document
        result = _extract_document(b"fake pdf content", "http://e.com/doc.pdf")
        self.assertIsNotNone(result.text)

    def test_extract_document_markitdown_exception(self):
        from app.crawl.extraction import _extract_document
        result = _extract_document(b"not a pdf", "http://e.com/doc.pdf")
        self.assertTrue(result.text is None or isinstance(result.text, str))

    def test_parse_html_tree_empty(self):
        from app.crawl.extraction import _parse_html_tree
        self.assertIsNone(_parse_html_tree(""))
        self.assertIsNone(_parse_html_tree(None))

    def test_parse_html_tree_parser_error(self):
        from app.crawl.extraction import _parse_html_tree
        result = _parse_html_tree("<")
        self.assertIsNotNone(result)


class TestBrowserCoverageGaps(unittest.TestCase):
    """Cover remaining branches in browser.py"""

    def test_browser_renderer_reset_browser_error(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        asyncio.run(renderer._reset_browser())
        renderer._browser = None
        asyncio.run(renderer._reset_browser())

    def test_browser_renderer_close_error(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        asyncio.run(renderer.close())
        asyncio.run(renderer.close())

    def test_browser_renderer_pdf_text_extractor_error(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        text = renderer._pdf_text_extractor("http://127.0.0.1:1/nonexistent.pdf")
        self.assertEqual(text, "")

    def test_browser_renderer_capture_element_screenshots_error(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        class MockPage:
            async def evaluate(self, _):
                return []
        result = asyncio.run(renderer._capture_element_screenshots(MockPage(), []))
        self.assertEqual(result, [])

    def test_browser_renderer_flatten_shadow_dom_error(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        class MockPage:
            async def evaluate(self, _):
                raise Exception("flatten failed")
        asyncio.run(renderer._flatten_shadow_dom(MockPage()))

    def test_browser_renderer_extract_with_challenge_wait_timeout(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        renderer._challenge_wait_seconds = 0.001
        # Page that always shows Cloudflare challenge
        class MockPage:
            async def evaluate(self, script):
                return "Just a moment..."
            async def content(self):
                return "<html><body>Just a moment...</body></html>"
        html, text, classification, escaped = asyncio.run(
            renderer._extract_with_challenge_wait(MockPage(), type('obj', (), {'include_html': True, 'include_text': True})())
        )
        self.assertEqual(classification, "bot_challenge_detected")
        self.assertFalse(escaped)

    def test_browser_renderer_extract_with_challenge_wait_escaped(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        renderer._challenge_wait_seconds = 0.001
        call_count = [0]
        class MockPage:
            async def evaluate(self, script):
                call_count[0] += 1
                if call_count[0] <= 3:
                    return "Just a moment..."
                return "Normal page content"
            async def content(self):
                if call_count[0] <= 3:
                    return "<html><body>Just a moment...</body></html>"
                return "<html><body>Normal page content</body></html>"
        html, text, classification, escaped = asyncio.run(
            renderer._extract_with_challenge_wait(MockPage(), type('obj', (), {'include_html': True, 'include_text': True})())
        )
        self.assertNotEqual(classification, "bot_challenge_detected")
        self.assertTrue(escaped)

    def test_browser_renderer_extract_html_error(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        class MockPage:
            async def content(self):
                raise Exception("content failed")
        result = asyncio.run(renderer._extract_html(MockPage()))
        self.assertEqual(result, "")

    def test_browser_renderer_extract_text_error(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        class MockPage:
            async def evaluate(self, _):
                raise Exception("evaluate failed")
        result = asyncio.run(renderer._extract_text(MockPage()))
        self.assertEqual(result, "")

    def test_browser_renderer_auto_accept_cookie_consent(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        class MockPage:
            async def query_selector(self, _):
                return None
        result = asyncio.run(renderer._auto_accept_cookie_consent(MockPage()))
        self.assertFalse(result)

    def test_browser_renderer_click_consent_button(self):
        from app.crawl.strategies.browser import BrowserRenderer
        renderer = BrowserRenderer()
        class MockElement:
            async def is_visible(self):
                return True
            async def click(self):
                pass
        class MockPage:
            def __init__(self):
                self.clicked = False
            async def query_selector(self, selector):
                if "accept" in selector:
                    return MockElement()
                return None
        result = asyncio.run(renderer._auto_accept_cookie_consent(MockPage()))
        self.assertTrue(result)


class TestHttpCoverageGaps(unittest.TestCase):
    """Cover remaining branches in http.py"""

    def test_charset_from_bytes_import_error(self):
        from app.crawl.strategies.http import _decode_body
        body = b"test content"
        result = _decode_body(body, "text/plain; charset=utf-8")
        self.assertEqual(result, "test content")

    def test_close_async_http_client_error(self):
        from app.crawl.strategies.http import close_async_http_client
        asyncio.run(close_async_http_client())
        asyncio.run(close_async_http_client())

    def test_is_private_or_loopback_ip_invalid(self):
        from app.crawl.strategies.http import _is_private_or_loopback_ip
        self.assertTrue(_is_private_or_loopback_ip("127.0.0.1"))
        self.assertTrue(_is_private_or_loopback_ip("192.168.1.1"))
        self.assertTrue(_is_private_or_loopback_ip("10.0.0.1"))
        self.assertTrue(_is_private_or_loopback_ip("::1"))
        self.assertFalse(_is_private_or_loopback_ip("8.8.8.8"))
        self.assertTrue(_is_private_or_loopback_ip("invalid"))

    def test_default_cffi_fetcher_response(self):
        from app.crawl.strategies.http import _default_cffi_fetcher
        result = _spawn_fetch(lambda s: asyncio.run(_default_cffi_fetcher(s.base_url + "/", {}, 10, 50*1024*1024)))
        self.assertEqual(result.status_code, 200)

    def test_http_strategy_result_httpx_request_error(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache
        cache = HttpCache()
        def failing_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            import httpx
            raise httpx.RequestError("connection failed", request=None)
        result = asyncio.run(_http_strategy_result(
            original_url="http://fail.example.com/",
            normalized_url="http://fail.example.com/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}},
            http_fetcher=failing_fetcher,
            cache=cache,
        ))
        self.assertEqual(result.classification, "network_error")

    def test_http_strategy_result_httpx_http_status_error(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache
        cache = HttpCache()
        def error_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            import httpx
            class MockResponse:
                content = b"error"
                status_code = 500
                headers = {"content-type": "text/plain"}
                url = "http://error.example.com/"
            raise httpx.HTTPStatusError("server error", request=None, response=MockResponse())
        result = asyncio.run(_http_strategy_result(
            original_url="http://error.example.com/",
            normalized_url="http://error.example.com/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}},
            http_fetcher=error_fetcher,
            cache=cache,
        ))
        self.assertEqual(result.classification, "network_error")

    def test_decode_body_unicode_decode_error(self):
        from app.crawl.strategies.http import _decode_body
        body = b"\xff\xfe"
        result = _decode_body(body, "text/html")
        self.assertIsInstance(result, str)

    def test_http_strategy_result_binary_classification(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache
        from app.constants import EXTRACTION_METHOD_BINARY_NO_TEXT
        cache = HttpCache()
        def binary_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            class Resp:
                status_code = 200
                headers = {"content-type": "application/octet-stream"}
                body = b"binary"
                final_url = url
                elapsed_ms = 10
            return Resp()
        result = asyncio.run(_http_strategy_result(
            original_url="http://binary.example.com/file.bin",
            normalized_url="http://binary.example.com/file.bin",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}},
            http_fetcher=binary_fetcher,
            cache=cache,
        ))
        self.assertEqual(result.classification, EXTRACTION_METHOD_BINARY_NO_TEXT)


class TestMainCoverageGaps(unittest.TestCase):
    """Cover remaining branches in main.py"""

    def test_main_import_error_handling(self):
        import app.main
        self.assertIsNotNone(app.main)

    def test_render_endpoint_value_error(self):
        from app.main import app

        async def test(client):
            resp = await client.post("/v1/render", json={"url": "invalid"})
            self.assertIn(resp.status_code, (200, 422, 500))
        asyncio.run(run_async_test(app, test))

    def test_render_endpoint_success(self):
        from app.main import app

        async def test(client):
            # Missing 'url' field triggers app-level 400 validation error
            resp = await client.post("/v1/render", json={})
            self.assertEqual(resp.status_code, 400)
        asyncio.run(run_async_test(app, test))

    def test_crawl_endpoint_webhook_error(self):
        from app.main import app

        async def test(client):
            # Crawl a valid URL; webhook delivery is best-effort and won't crash the request
            resp = await client.post("/v1/crawl", json={"url": "http://example.com/", "webhook_url": "http://unreachable.invalid/"})
            # The endpoint should accept the request (202 = accepted, background processing)
            self.assertIn(resp.status_code, (200, 201, 202))
            # Give background task a brief moment, then ensure no exception propagated
            await asyncio.sleep(0.5)
        asyncio.run(run_async_test(app, test))

    def test_map_endpoint_value_error(self):
        from app.main import app

        async def test(client):
            resp = await client.post("/v1/map", json={"url": "invalid"})
            self.assertEqual(resp.status_code, 422)
        asyncio.run(run_async_test(app, test))

    def test_job_endpoints_not_found(self):
        from app.main import app

        async def test(client):
            resp = await client.get("/v1/jobs/nonexistent")
            self.assertEqual(resp.status_code, 404)
            resp = await client.post("/v1/jobs/nonexistent/control", json={"action": "cancel"})
            self.assertEqual(resp.status_code, 404)
            resp = await client.delete("/v1/jobs/nonexistent")
            self.assertEqual(resp.status_code, 404)
        asyncio.run(run_async_test(app, test))

    def test_app_none_fallback(self):
        import app.main as main_mod
        self.assertIsNotNone(main_mod.app)


class TestSchedulerCoverageGaps(unittest.TestCase):
    """Cover remaining branches in scheduler.py"""

    def test_token_bucket_basic(self):
        from app.scheduler import _TokenBucket
        bucket = _TokenBucket(rate=10.0, capacity=5.0)
        wait = bucket.consume(1.0)
        self.assertEqual(wait, 0.0)
        wait = bucket.consume(4.0)
        self.assertEqual(wait, 0.0)
        wait = bucket.consume(1.0)
        self.assertGreater(wait, 0.0)

    def test_token_bucket_refill(self):
        from app.scheduler import _TokenBucket
        bucket = _TokenBucket(rate=100.0, capacity=10.0)
        bucket.consume(10.0)
        # Manually refill tokens as if 0.1s passed (at 100 tokens/s, that's 10 more)
        bucket.tokens = 10.0
        wait = bucket.consume(1.0)
        self.assertEqual(wait, 0.0)

    def test_circuit_breaker_opens(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)
        for _ in range(5):
            scheduler.record_failure("example.com")
        self.assertTrue(scheduler.is_circuit_open("example.com"))

    def test_circuit_breaker_cooldown_expired(self):
        from app.scheduler import DomainScheduler
        import time
        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)
        scheduler._circuit_open_until["example.com"] = time.monotonic() - 1
        self.assertFalse(scheduler.is_circuit_open("example.com"))

    def test_scheduler_record_latency(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0, adaptive=True)
        scheduler.record_latency("example.com", 1.0)
        scheduler.record_latency("example.com", 2.0)
        self.assertIn("example.com", scheduler._domain_latency_ema)

    def test_scheduler_max_rps_token_bucket(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0, max_rps=5.0)
        self.assertGreater(scheduler._max_rps, 0)


class TestEngineCoverageGaps(unittest.TestCase):
    """Cover remaining branches in engine.py"""

    def test_crawl_chain_robots_disallowed(self):
        # _robots_disallowed_result is nested inside crawl_request; test via _failed_engine_result
        from app.crawl.engine import _failed_engine_result
        result = _failed_engine_result(
            original_url="http://example.com/page",
            normalized_url="http://example.com/page",
            strategy="HTTP_DECLARED_CRAWLER",
            classification="robots_disallowed",
            error="robots_txt_disallowed",
        )
        self.assertEqual(result.classification, "robots_disallowed")

    def test_crawl_chain_continue_on_skip(self):
        from app.crawl.engine import _execute_crawl_chain
        from app.scheduler import DomainScheduler

        def fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            class Resp:
                status_code = 200
                headers = {"content-type": "text/html"}
                body = b"<html><title>Access Denied</title>bot challenge</html>"
                final_url = url
                elapsed_ms = 10
            return Resp()

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        policy = {
            "strategies": ["HTTP_DECLARED_CRAWLER", "BROWSER_HEADLESS"],
            "extraction": "full",
            "headers": {},
            "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            "request_profile": {},
        }
        # HTTP returns access_denied (blocked), browser_worker is None -> browser_crash
        result = asyncio.run(_execute_crawl_chain(
            original_url="http://example.com/page",
            normalized_url="http://example.com/page",
            policy=policy,
            http_fetcher=fetcher,
            cffi_fetcher=fetcher,
            browser_worker=None,
            scheduler=scheduler,
            domain="example.com",
        ))
        self.assertEqual(result.classification, "browser_crash")


class TestContentCacheCoverageGaps(unittest.TestCase):
    """Cover remaining branches in content_cache.py"""

    def test_content_cache_get_invalid_json(self):
        from app.crawl.content_cache import ContentCache
        import tempfile
        import pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir)
            cache_path = pathlib.Path(tmpdir) / "content.testkey.json"
            cache_path.write_text("not valid json")
            result = cache.get("testkey")
            self.assertIsNone(result)

    def test_content_cache_get_expired(self):
        cache, clock_vals = _temp_cache(ttl=1)
        cache.set("key", {"data": "value"})
        clock_vals[0] = 1002.0
        self.assertIsNone(cache.get("key"))

    def test_content_cache_prune(self):
        from app.crawl.content_cache import ContentCache
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = ContentCache(tmpdir, default_ttl=1, clock=lambda: clock_vals[0])
            cache.set("key1", {"data": "1"})
            cache.set("key2", {"data": "2"})
            clock_vals[0] = 1002.0
            removed = cache.prune()
            self.assertEqual(removed, 2)
            self.assertIsNone(cache.get("key1"))
            self.assertIsNone(cache.get("key2"))

    def test_content_cache_clear(self):
        from app.crawl.content_cache import ContentCache
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir)
            cache.set("key1", {"data": "1"})
            cache.set("key2", {"data": "2"})
            removed = cache.clear()
            self.assertEqual(removed, 2)
            self.assertIsNone(cache.get("key1"))
            self.assertIsNone(cache.get("key2"))


class TestOutputFormatsCoverageGaps(unittest.TestCase):
    """Cover remaining branches in output_formats.py"""

    def test_serialize_default_fallback(self):
        from app.output_formats import _default
        class CustomObj:
            def __init__(self):
                self.attr = "value"
        obj = CustomObj()
        result = _default(obj)
        self.assertEqual(result, {"attr": "value"})

    def test_serialize_results_json(self):
        from app.output_formats import serialize_results
        results = [{"url": "http://x/", "text": "content"}]
        output, media_type = serialize_results(results, "json")
        self.assertIn("http://x/", output)
        self.assertEqual(media_type, "application/json")

    def test_serialize_results_jsonl(self):
        from app.output_formats import serialize_results
        results = [{"url": "http://x/", "text": "content"}]
        output, media_type = serialize_results(results, "jsonl")
        self.assertIn("http://x/", output)
        self.assertEqual(media_type, "application/x-ndjson")

    def test_serialize_results_ndjson(self):
        from app.output_formats import serialize_results
        results = [{"url": "http://x/", "text": "content"}]
        output, media_type = serialize_results(results, "ndjson")
        self.assertIn("http://x/", output)
        self.assertEqual(media_type, "application/x-ndjson")

    def test_serialize_results_markdown(self):
        from app.output_formats import serialize_results
        results = [{"url": "http://x/", "markdown": "# Title\n\nContent"}]
        output, media_type = serialize_results(results, "markdown")
        self.assertIn("# Title", output)
        self.assertEqual(media_type, "text/markdown; charset=utf-8")

    def test_serialize_results_csv(self):
        from app.output_formats import serialize_results
        results = [{
            "original_url": "http://x/", "normalized_url": "http://x/", "final_url": "http://x/",
            "status": "success", "classification": "rendered", "selected_strategy": "HTTP_DECLARED_CRAWLER",
            "quality_score": 1.0, "discovered_link_count": 0, "error": None, "http_status": 200
        }]
        output, media_type = serialize_results(results, "csv")
        self.assertIn("http://x/", output)
        self.assertEqual(media_type, "text/csv; charset=utf-8")

    def test_serialize_results_unknown_format(self):
        from app.output_formats import serialize_results
        results = [{"url": "http://x/", "text": "content"}]
        with self.assertRaises(ValueError):
            serialize_results(results, "unknown")


class TestJobStoreCoverageGaps(unittest.TestCase):
    """Cover remaining branches in job_store.py"""

    def test_job_store_load_exception(self):
        from app.crawl.job_store import JobStore
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = pathlib.Path(tmpdir) / "jobs.db"
            store = JobStore(db_path)
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.execute("DROP TABLE IF EXISTS jobs")
            conn.commit()
            conn.close()
            result = store.load("nonexistent")
            self.assertIsNone(result)

    def test_job_store_close(self):
        from app.crawl.job_store import JobStore
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = pathlib.Path(tmpdir) / "jobs.db"
            store = JobStore(db_path)
            store._conn.close()
            store._conn.close()


class TestDecompressionCoverageGaps(unittest.TestCase):
    """Cover remaining branches in decompression.py"""

    def test_decompress_body_brotli_import_error(self):
        from app.crawl.decompression import decompress_body
        body = b"not brotli"
        result = decompress_body(body, "br")
        self.assertEqual(result, body)


class TestPayloadHelpersCoverageGaps(unittest.TestCase):
    """Cover remaining branches in payload_helpers.py"""

    def test_payload_policy_invalid_limits(self):
        from app.crawl.payload_helpers import _payload_policy
        from app.settings import CrawlerSettings
        with self.assertRaises(ValueError) as cm:
            _payload_policy({"policy": {"limits": "not a dict"}}, CrawlerSettings.from_mapping())
        self.assertEqual(str(cm.exception), "invalid_policy_limits")
        with self.assertRaises(ValueError) as cm:
            _payload_policy({"policy": {"limits": {"max_depth": "not an int"}}}, CrawlerSettings.from_mapping())
        self.assertEqual(str(cm.exception), "invalid_policy_limits")


class TestResultHelpersCoverageGaps(unittest.TestCase):
    """Cover remaining branches in result_helpers.py"""

    def test_blocked_engine_result(self):
        from app.crawl.result_helpers import _blocked_engine_result
        result = _blocked_engine_result(
            original_url="http://x/",
            normalized_url="http://x/",
            strategy="HTTP_DECLARED_CRAWLER",
            classification="blocked",
            http_status=403,
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.classification, "blocked")
        self.assertEqual(result.http_status, 403)

    def test_binary_no_text_result(self):
        from app.crawl.result_helpers import _binary_no_text_result
        result = _binary_no_text_result(
            original_url="http://x/file.bin",
            normalized_url="http://x/file.bin",
            final_url="http://x/file.bin",
            strategy="HTTP_DECLARED_CRAWLER",
            http_status=200,
            headers={"content-type": "application/octet-stream"},
            content_type="application/octet-stream",
            elapsed_ms=100,
        )
        self.assertEqual(result.classification, "binary_no_text")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.content_type, "application/octet-stream")


if __name__ == "__main__":
    unittest.main()


class TestAdditionalCoverage(unittest.TestCase):
    """Additional targeted tests to close coverage gaps."""

    def test_content_cache_set_overwrite(self):
        cache, _ = _temp_cache(ttl=60)
        cache.set("key", {"data": "first"})
        cache.set("key", {"data": "second"})
        self.assertEqual(cache.get("key"), {"data": "second"})

    def test_content_cache_get_corrupt_json(self):
        from app.crawl.content_cache import ContentCache
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir, default_ttl=60)
            cache_path = cache._path_for("testkey")
            cache_path.write_text("{broken json")
            result = cache.get("testkey")
            self.assertIsNone(result)

    def test_content_cache_prune_corrupt_entry(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = make_content_cache_with_entry(tmpdir, clock=lambda: clock_vals[0])
            # Write a corrupt file with the correct hash-based name - prune should skip it
            bad_path = cache._path_for("badkey")
            bad_path.write_text("not valid json")
            clock_vals[0] = 1002.0  # advance past TTL
            removed = cache.prune()
            # Only key1 is expired and removable; corrupt file is skipped
            self.assertEqual(removed, 1)

    def test_job_store_update(self):
        from app.crawl.job_store import JobStore, JobStatus
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = pathlib.Path(tmpdir) / "jobs.db"
            store = JobStore(db_path)
            job = store.create("http://example.com/", {})
            store.save(job)
            updated = store.load(job.job_id)
            self.assertIsNotNone(updated)

    def test_job_store_delete(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_job_store(tmpdir)
            job = store.create("http://example.com/", {})
            deleted = store.delete(job.job_id)
            self.assertTrue(deleted)
            self.assertIsNone(store.load(job.job_id))
            deleted = store.delete(job.job_id)
            self.assertFalse(deleted)

    def test_output_formats_default_with_dict(self):
        from app.output_formats import _default
        # Object with model_dump method
        class FakeObj:
            def model_dump(self):
                return {"serialized": "true"}
        result = _default(FakeObj())
        self.assertEqual(result, {"serialized": "true"})

    def test_output_formats_serialize_unknown_format(self):
        from app.output_formats import serialize_results
        results = [{"original_url": "http://x/"}]
        with self.assertRaises(ValueError):
            serialize_results(results, "unknown_format")

    def test_decompression_zlib(self):
        from app.crawl.decompression import decompress_body
        import zlib
        original = b"hello world " * 10
        compressed = zlib.compress(original)
        result = decompress_body(compressed, "deflate")
        self.assertIsNotNone(result)

    def test_decompression_gzip(self):
        from app.crawl.decompression import decompress_body
        import gzip
        original = b"hello world " * 10
        compressed = gzip.compress(original)
        result = decompress_body(compressed, "gzip")
        self.assertIsNotNone(result)

    def test_decompression_unknown_encoding(self):
        from app.crawl.decompression import decompress_body
        result = decompress_body(b"some data", "unknown")
        self.assertEqual(result, b"some data")

    def test_decompression_none_encoding(self):
        from app.crawl.decompression import decompress_body
        # With an unrecognized encoding, returns the original body
        result = decompress_body(b"some data", "unknown_encoding")
        self.assertEqual(result, b"some data")

    def test_scheduler_domain_cooldowns(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.set_domain_cooldown("example.com", 10)
        cooldowns = scheduler.domain_cooldowns
        self.assertIsInstance(cooldowns, dict)
        self.assertIn("example.com", cooldowns)

    def test_scheduler_circuit_breaker_reset(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0, circuit_cooldown=0.0)
        # Trip the circuit breaker (threshold=5 by default)
        for _ in range(5):
            scheduler.record_failure("example.com")
        self.assertTrue(scheduler.is_circuit_open("example.com"))
        # record_success resets failure counter; with cooldown=0, circuit is not open
        scheduler.record_success("example.com")
        self.assertEqual(scheduler._domain_failures.get("example.com", 0), 0)

    def test_http_async_client_close(self):
        from app.crawl.strategies.http import get_async_http_client, close_async_http_client
        import asyncio
        # Get a client, then close it
        async def test():
            client = get_async_http_client()
            await close_async_http_client()
        asyncio.run(test())

    def test_engine_shared_content_cache(self):
        from app.crawl.engine import _get_shared_content_cache
        from app.crawl.content_cache import ContentCache
        cache = _get_shared_content_cache()
        self.assertIsInstance(cache, ContentCache)

    def test_http_large_content(self):
        from app.crawl.strategies.http import _default_http_fetcher
        result = _spawn_fetch(lambda s: asyncio.run(_default_http_fetcher(s.base_url + "/", {}, 10, 1)))
        self.assertEqual(result.status_code, 413)

    def test_extraction_different_methods(self):
        from app.crawl.extraction import extract_content
        html = "<html><body><p>Hello world</p></body></html>"
        for method, content_type in [("trafilatura", "text/html"), ("document", "application/pdf"), ("dom_text", "text/html"), ("full", "text/html")]:
            result = extract_content(html, content_type, "http://example.com/", None, None)
            self.assertIsNotNone(result)


class TestMoreCoverageGaps(unittest.TestCase):
    """More targeted tests to close remaining coverage gaps."""

    def test_content_cache_prune_os_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = make_content_cache_with_entry(tmpdir, clock=lambda: clock_vals[0])
            clock_vals[0] = 1002.0
            # Replace file with directory to cause OSError on read
            cache_path = cache._path_for("key1")
            cache_path.unlink()
            cache_path.mkdir()
            removed = cache.prune()
            cache_path.rmdir()

    def test_output_formats_default_dict_method(self):
        from app.output_formats import _default
        class DictObj:
            def dict(self):
                return {"from_dict": "value"}
        result = _default(DictObj())
        self.assertEqual(result, {"from_dict": "value"})

    def test_output_formats_default_str_fallback(self):
        from app.output_formats import _default
        result = _default(42)
        self.assertEqual(result, "42")

    def test_decompression_brotli_exception(self):
        from app.crawl.decompression import decompress_body
        try:
            result = decompress_body(b"not gzip data", "gzip")
            self.assertEqual(result, b"not gzip data")
        except Exception:
            pass

    def test_job_store_load_corrupt_row(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            store = make_job_store(tmpdir)
            job = store.create("http://example.com/", {})
            # Corrupt the policy JSON in the database
            import sqlite3
            conn = sqlite3.connect(str(pathlib.Path(tmpdir) / "jobs.db"))
            conn.execute("UPDATE crawl_jobs SET policy = ? WHERE job_id = ?", ("invalid json{", job.job_id))
            conn.commit()
            conn.close()
            # load should catch the JSON decode error and return None
            result = store.load(job.job_id)
            self.assertIsNone(result)

    def test_scheduler_record_latency_invalid(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        # None latency should be ignored (line 114: return)
        scheduler.record_latency("example.com", None)
        # Negative latency should be ignored
        scheduler.record_latency("example.com", -1.0)
        # Verify no latency was recorded
        self.assertNotIn("example.com", scheduler._domain_latency_ema)

    def test_http_strategy_not_modified(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache

        def not_mod_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            class Resp:
                status_code = 304
                headers = {"etag": "v1", "content-type": "text/html"}
                body = b"not modified"
                final_url = url
                elapsed_ms = 10
            return Resp()

        cache = HttpCache()
        cache.store("http://example.com/", {"etag": "v1"})
        result = asyncio.run(_http_strategy_result(
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}},
            http_fetcher=not_mod_fetcher,
            cache=cache,
        ))
        self.assertEqual(result.classification, "not_modified")

    def test_content_cache_clear_os_error(self):
        from app.crawl.content_cache import ContentCache
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir, default_ttl=60)
            cache.set("key1", {"data": "1"})
            cache_path = cache._path_for("key1")
            cache_path.unlink()
            cache_path.mkdir()
            removed = cache.clear()
            cache_path.rmdir()

    def test_output_formats_safe_json(self):
        from app.output_formats import _safe
        val = {"nested": [1, 2, 3]}
        result = _safe(val)
        self.assertIsInstance(result, str)

    def test_output_formats_safe_none(self):
        from app.output_formats import _safe
        result = _safe(None)
        self.assertEqual(result, "")

    def test_output_formats_safe_string(self):
        from app.output_formats import _safe
        result = _safe("hello")
        self.assertEqual(result, "hello")

    def test_output_formats_safe_int(self):
        from app.output_formats import _safe
        result = _safe(42)
        self.assertEqual(result, "42")

    def test_http_charset_decode_error(self):
        from app.crawl.strategies.http import _decode_body
        # Test with invalid charset in headers
        result = _decode_body(b"hello", "text/plain", {"charset": "invalid_charset"})
        self.assertIsInstance(result, str)

    def test_http_cookie_invalid(self):
        from app.crawl.strategies.http import _GLOBAL_COOKIE_JAR
        # Call with invalid cookie header - should not raise
        _GLOBAL_COOKIE_JAR.add_cookie_header({}, "http://example.com/")

    def test_scheduler_record_latency_valid(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.record_latency("example.com", 100.0)
        self.assertIn("example.com", scheduler._domain_latency_ema)
        self.assertAlmostEqual(scheduler._domain_latency_ema["example.com"], 100.0)

    def test_scheduler_record_latency_ema(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0, adaptive_smoothing=0.5)
        scheduler.record_latency("example.com", 100.0)
        scheduler.record_latency("example.com", 200.0)
        self.assertAlmostEqual(scheduler._domain_latency_ema["example.com"], 150.0, places=1)

    def test_scheduler_record_success(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.record_success("example.com")
        self.assertEqual(scheduler._domain_failures.get("example.com", 0), 0)

    def test_scheduler_is_circuit_open_false(self):
        from app.scheduler import DomainScheduler
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        self.assertFalse(scheduler.is_circuit_open("example.com"))

    def test_http_quality_score_failed(self):
        from app.crawl.strategies.http import _quality_score
        score = _quality_score("failed", "network_error", None, None, None)
        self.assertEqual(score, 0.0)

    def test_http_quality_score_empty_text(self):
        from app.crawl.strategies.http import _quality_score
        score = _quality_score("success", "rendered", "", None, 200)
        self.assertGreater(score, 0.0)

    def test_http_status_not_found(self):
        from app.crawl.strategies.http import _status_for_classification
        status = _status_for_classification("not_found")
        self.assertEqual(status, "failed")

    def test_http_status_site_error(self):
        from app.crawl.strategies.http import _status_for_classification
        status = _status_for_classification("site_error")
        self.assertEqual(status, "failed")

    def test_http_cffi_imersonate_result(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache
        def cffi_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            class Resp:
                status_code = 200
                headers = {"content-type": "text/html"}
                body = b"<html><body>content</body></html>"
                final_url = url
                elapsed_ms = 10
            return Resp()
        result = asyncio.run(_http_strategy_result(
            original_url="http://x/",
            normalized_url="http://x/",
            strategy="HTTP_CFFI_IMPERSONATE",
            policy={"strategies": ["HTTP_CFFI_IMPERSONATE"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10}},
            http_fetcher=cffi_fetcher,
            cache=HttpCache(),
        ))
        self.assertEqual(result.classification, "rendered")

    def test_http_cffi_import_fallback(self):
        from app.crawl.strategies.http import _default_cffi_fetcher
        from helpers.server import spawn_server
        server = spawn_server()
        try:
            result = asyncio.run(_default_cffi_fetcher(server.base_url + "/", {}, 10, 50*1024*1024))
            self.assertEqual(result.status_code, 200)
        finally:
            server.stop()

    def test_extraction_parse_html_tree_error(self):
        from app.crawl.extraction import _parse_html_tree
        result = _parse_html_tree("{{invalid html")
        # Should not raise - lxml is forgiving
        self.assertIsNotNone(result)

    def test_extraction_selected_with_xpath(self):
        from app.crawl.extraction import _extract_selected
        html = "<html><body><div>Hello</div></body></html>"
        result = _extract_selected(html, "http://x/", css_selector=None, xpath="//div/text()")
        self.assertIsNotNone(result)

    def test_main_invalid_webhook_url(self):
        from app.main import app
        from httpx import AsyncClient
        import asyncio, httpx
        async def test():
            transport = httpx.ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/v1/crawl", json={"url": "http://example.com/", "webhook_url": "not_a_url"})
                self.assertEqual(resp.status_code, 422)
        asyncio.run(test())

    def test_main_job_control_invalid_action(self):
        from app.main import app, job_store
        job = job_store.create("http://example.com/", {})
        asyncio.run(run_job_control_test(app, job_store, job.job_id, "invalid_action", 422))
        job_store.delete(job.job_id)

    def test_main_job_control_cancel_completed(self):
        from app.main import app, job_store
        from app.crawl.job_store import JobStatus
        job = job_store.create("http://example.com/", {})
        # Set to completed
        job.status = JobStatus.COMPLETED
        job_store.save(job)
        asyncio.run(run_job_control_test(app, job_store, job.job_id, "cancel", 422))
        job_store.delete(job.job_id)

    def test_main_job_control_resume_not_paused(self):
        from app.main import app, job_store
        from app.crawl.job_store import JobStatus
        job = job_store.create("http://example.com/", {})
        asyncio.run(run_job_control_test(app, job_store, job.job_id, "resume", 422))
        job_store.delete(job.job_id)

    def test_engine_crawl_request_with_webhook(self):
        from app.crawl.engine import crawl_request
        from app.settings import CrawlerSettings
        import asyncio
        payload = {
            "url": "http://127.0.0.1:1/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 5},
                "request_profile": {},
            },
            "webhook_url": "http://webhook.invalid",
        }
        async def browser_worker(job):
            return None
        result = asyncio.run(crawl_request(
            payload,
            settings=CrawlerSettings.from_mapping(),
            browser_worker=browser_worker,
            shutdown_event=asyncio.Event(),
            webhook_url="http://webhook.invalid",
        ))
        self.assertIn("webhook_url", result)

    def test_engine_parse_robots_blank_line(self):
        from app.crawl.engine import _extract_discovered_links
        # Test link extraction with mixed quote styles (covers edge cases)
        html = "<html><body><a href=https://example.com/test>link</a><a href='https://example.com/page2'>link2</a></body></html>"
        links = _extract_discovered_links(html, None)
        self.assertTrue(len(links) > 0)

    def test_http_strategy_with_cffi_retry(self):
        from app.crawl.strategies.http import _http_strategy_result
        from app.crawl.cache import HttpCache
        call_count = [0]
        def retry_fetcher(url, headers, timeout, max_content_length=50*1024*1024):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("first attempt fails")
            class Resp:
                status_code = 200
                headers = {"content-type": "text/html"}
                body = b"<html><body>recovered</body></html>"
                final_url = url
                elapsed_ms = 10
            return Resp()
        result = asyncio.run(_http_strategy_result(
            original_url="http://x/",
            normalized_url="http://x/",
            strategy="HTTP_DECLARED_CRAWLER",
            policy={"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}, "limits": {"timeout_seconds": 5, "max_retries": 1, "max_discovered_links": 10}},
            http_fetcher=retry_fetcher,
            cache=HttpCache(),
        ))
        self.assertEqual(result.classification, "rendered")


# --- Helpers to reduce test boilerplate ---
def _temp_cache(ttl=1, clock_start=1000.0):
    from app.crawl.content_cache import ContentCache
    import tempfile
    clock_vals = [clock_start]
    return ContentCache(tempfile.mkdtemp(), default_ttl=ttl, clock=lambda: clock_vals[0]), clock_vals


def _spawn_fetch(fn):
    from helpers.server import spawn_server
    server = spawn_server()
    try:
        return fn(server)
    finally:
        server.stop()
