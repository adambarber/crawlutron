"""Real tests for service.py logic (no mocks; inject fakes for network)."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("BROWSER_CRAWLER_LOG_FILE", str(Path(tempfile.gettempdir()) / "browser-crawler-test.jsonl"))

from app.models import CrawlStrategyAttempt, HttpFetchResult, UniversalCrawlResult  # noqa: E402
from app.service import (  # noqa: E402
    _collect_document_item_lines,
    _cookie_header_for_url,
    _default_document_text_extractor,
    _default_trafilatura_extractor,
    _document_item_range,
    _execute_crawl_chain,
    _call_worker,
    _extract_discovered_links,
    _extract_forms,
    _extract_frames,
    _fetch_with_retries,
    _format_extracted_document_text,
    _FrameCollector,
    _headers_for_strategy,
    _http_strategy_result,
    _looks_like_binary_body,
    _next_non_empty_document_line,
    _payload_policy,
    _payload_urls,
    _render_options,
    _retry_after_seconds,
    _should_join_document_paragraph,
    crawl_request,
)
from app.settings import CrawlerSettings  # noqa: E402

def make_fetch(status=200, body=b"<html><body>hello</body></html>", ctype="text/html; charset=utf-8",
               final_url="http://example.com/", url="http://example.com/"):
    return HttpFetchResult(url=url, final_url=final_url, status_code=status,
                           headers={"content-type": ctype}, body=body, elapsed_ms=1)

def ok_fetcher(url, headers, timeout):
    return make_fetch(url=url)

def raise_fetcher(url, headers, timeout):
    raise ConnectionError("network down")

def discovery_fetcher(url, headers, timeout):
    if url.endswith("/robots.txt") or url.endswith("/sitemap.xml"):
        raise ConnectionError("discovery failed")
    return make_fetch()

BASE_POLICY = {
    "schema_version": "2026-06-23",
    "strategies": ["HTTP_DECLARED_CRAWLER"],
    "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
    "headers": {},
    "request_profile": {},
    "browser_profile": {},
    "discovery": {},
}

class TestValidationHelpers:
    def test_payload_urls_errors(self) -> None:
        try:
            _payload_urls({"urls": "notalist"})
            assert False, "expected ValueError"
        except ValueError as exc:
            assert str(exc) == "invalid_urls"
        try:
            _payload_urls({})
            assert False, "expected ValueError"
        except ValueError as exc:
            assert str(exc) == "missing_url"
        assert _payload_urls({"url": "http://x/"}) == ["http://x/"]
        assert _payload_urls({"urls": ["http://x/", "http://y/"]}) == ["http://x/", "http://y/"]

    def test_render_options_error(self) -> None:
        try:
            _render_options({"options": "bad"}, CrawlerSettings.from_mapping())
            assert False, "expected ValueError"
        except ValueError as exc:
            assert str(exc) == "invalid_options"

    def test_payload_policy_errors(self) -> None:
        s = CrawlerSettings.from_mapping()
        cases = [
            ({"policy": "x"}, "invalid_policy"),
            ({"policy": {"limits": "x"}}, "invalid_policy_limits"),
            ({"policy": {"headers": "x"}}, "invalid_policy_headers"),
            ({"policy": {"strategies": "x"}}, "invalid_policy_strategies"),
            ({"policy": {"request_profile": "x"}}, "invalid_policy_profile"),
        ]
        for payload, expected in cases:
            try:
                _payload_policy(payload, s)
                assert False, f"expected ValueError for {payload}"
            except ValueError as exc:
                assert str(exc) == expected

    def test_retry_after_seconds_invalid(self) -> None:
        assert _retry_after_seconds({"retry-after": "notanint"}) is None
        assert _retry_after_seconds({"retry-after": "30"}) == 30
        assert _retry_after_seconds({}) is None

class TestHeaderHelpers:
    def test_mobile_safari_user_agent(self) -> None:
        policy = {"request_profile": {"user_agent_family": "mobile_safari"}, "headers": {}}
        headers = _headers_for_strategy("HTTP_DECLARED_CRAWLER", policy, "http://x/")
        assert "iPhone" in headers["User-Agent"]

    def test_cookie_header_invalid_structures(self) -> None:
        assert _cookie_header_for_url({"cookie_jar": "per_host", "cookies": "notdict"}, "http://host/") == ""
        assert _cookie_header_for_url({"cookie_jar": "per_host", "cookies": {"host": "notdict"}}, "http://host/") == ""

class TestFetchRetries:
    def test_fetch_with_retries_reraises_after_exhaustion(self) -> None:
        try:
            _fetch_with_retries(raise_fetcher, "http://x/", {}, 5, 0)
            assert False, "expected re-raise"
        except ConnectionError:
            pass

class TestPureParsing:
    def test_document_item_range_branches(self) -> None:
        assert _document_item_range("1a", "5a") == ["1a", "5a"]
        assert _document_item_range("1a", "1e") == ["1a", "1b", "1c", "1d", "1e"]
        assert _document_item_range("1a", "5b") == ["1a", "5b"]
        assert _document_item_range("1a", "x") == ["1a", "x"]

    def test_looks_like_binary_body_branches(self) -> None:
        assert _looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", "http://x/z.zip") is True
        assert _looks_like_binary_body(b"PK\x03\x04data", "text/html", "http://x/") is False
        assert _looks_like_binary_body(b"data", "application/json", "http://x/") is False
        assert _looks_like_binary_body(b"data", "application/octet-stream", "http://x/") is True
        assert _looks_like_binary_body(b"data", "application/pdf", "http://x/a.pdf") is False

    def test_default_document_text_extractor_non_text_returns_empty(self) -> None:
        assert _default_document_text_extractor(b"data", "application/octet-stream", "http://x/z.bin") == ""
        assert _default_document_text_extractor(b"<p>hi</p>", "text/html", "http://x/") == "<p>hi</p>"

    def test_default_trafilatura_extractor_returns_string(self) -> None:
        assert isinstance(_default_trafilatura_extractor("<p>x</p>", "http://x/"), str)

    def test_format_extracted_document_text_range_action(self) -> None:
        text = "1a - 1c Approve\n1a.\nBuild the thing\n1b.\nFix the bug\n1c.\nShip it"
        out = _format_extracted_document_text(text, source_url="http://x/")
        assert "Approve" in out

    def test_should_join_document_paragraph_heading(self) -> None:
        assert _should_join_document_paragraph("Section A", "some prose") is False

    def test_next_non_empty_document_line_none(self) -> None:
        assert _next_non_empty_document_line(["", ""], 0) is None

    def test_collect_document_item_lines_breaks_on_section(self) -> None:
        lines = ["Build the thing", "1.", "Next section"]
        _, idx = _collect_document_item_lines(lines, 0)
        assert idx == 3

    def test_extract_frames_skips_bad_and_dupes(self) -> None:
        html = (
            '<iframe src="http://same.example/f"></iframe>'
            '<iframe src="javascript:alert(1)"></iframe>'
            '<iframe src="http://same.example/f"></iframe>'
        )
        frames = _extract_frames(html, "http://same.example/")
        assert len(frames) == 1

    def test_extract_discovered_links_skips_bad_scheme(self) -> None:
        html = '<a href="mailto:a@b.com">x</a><a href="http://other.example/p">y</a>'
        links, count, truncated = _extract_discovered_links(html, "http://example.com/")
        assert len(links) == 1

    def test_frame_collector_ignores_frames_without_src(self) -> None:
        collector = _FrameCollector()
        collector.feed('<frame></frame><iframe src="http://x/f"></iframe>')
        assert len(collector.frames) == 1

class TestHttpStrategyResult:
    def _run(self, status=200, strategy="HTTP_DECLARED_CRAWLER", fetcher=None,
             ctype="text/html; charset=utf-8", body=b"<html><body>hello</body></html>",
             final_url="http://example.com/", policy=None):
        if fetcher is None:
            def fetcher(url, headers, timeout):  # noqa: F811
                return make_fetch(status=status, ctype=ctype, body=body, final_url=final_url, url=url)
        pol = dict(BASE_POLICY)
        if policy:
            pol.update(policy)
        return _http_strategy_result(
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            strategy=strategy,
            policy=pol,
            http_fetcher=fetcher,
            document_text_extractor=lambda b, c, u: "DOC",
            trafilatura_extractor=lambda h, u: "TRA",
        )

    def test_success_html(self) -> None:
        assert self._run().status == "success"

    def test_status_classification_overrides(self) -> None:
        for status, classification in [(403, "access_denied"), (404, "not_found"),
                                       (410, "gone"), (429, "rate_limited"), (500, "site_error")]:
            assert self._run(status=status).classification == classification, status

    def test_network_error_handler(self) -> None:
        result = self._run(fetcher=raise_fetcher)
        assert result.status == "failed"
        assert result.classification == "network_error"

    def test_binary_body_short_circuits(self) -> None:
        result = self._run(
            ctype="application/octet-stream",
            body=b"PK\x03\x04data",
            final_url="http://example.com/a.zip",
        )
        assert result.classification == "binary_no_text"

    def test_document_extract_strategy(self) -> None:
        assert self._run(strategy="DOCUMENT_EXTRACT").text == "DOC"

    def test_trafilatura_extract_html_and_non_html(self) -> None:
        assert self._run(strategy="TRAFILATURA_EXTRACT").text == "TRA"
        non_html = self._run(strategy="TRAFILATURA_EXTRACT", ctype="application/json", body=b"{}")
        assert non_html.text == ""

    def test_discovery_diagnostics_records_exceptions_and_feeds(self) -> None:
        def feed_fetcher(url, headers, timeout):
            if url.endswith("/robots.txt") or url.endswith("/sitemap.xml"):
                raise ConnectionError("discovery failed")
            return make_fetch(body=b'<html><body><link rel="alternate" href="http://example.com/feed.xml"></body></html>')

        result = self._run(
            fetcher=feed_fetcher,
            policy={"discovery": {"robots": True, "sitemaps": True, "feeds": True}},
        )
        assert result.status == "success"
        assert "robots" in result.diagnostics
        assert result.diagnostics["robots"].get("error") is not None

class TestExecuteCrawlChain:
    async def _run_chain(self, strategy, **kwargs):
        return await _execute_crawl_chain(
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            policy=dict(BASE_POLICY, strategies=[strategy]),
            http_fetcher=ok_fetcher,
            browser_worker=None,
            document_text_extractor=lambda b, c, u: "DOC",
            trafilatura_extractor=lambda h, u: "TRA",
            **kwargs,
        )

    def test_http_strategy_runs(self) -> None:
        assert asyncio.run(self._run_chain("HTTP_DECLARED_CRAWLER")).status == "success"

    def test_browser_strategy_without_worker(self) -> None:
        assert asyncio.run(self._run_chain("BROWSER_HEADLESS")).classification == "browser_crash"

    def test_unsupported_strategy(self) -> None:
        assert asyncio.run(self._run_chain("NOT_A_STRATEGY")).classification == "unknown_error"

class TestCrawlRequest:
    def test_batch_size_exceeded(self) -> None:
        settings = CrawlerSettings.from_mapping()
        payload = {"urls": ["http://example.com/"] * (settings.max_batch_size + 1),
                   "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]}}
        try:
            asyncio.run(crawl_request(payload, settings=settings, http_fetcher=ok_fetcher,
                                       document_text_extractor=lambda b, c, u: "DOC",
                                       trafilatura_extractor=lambda h, u: "TRA", browser_worker=None))
            assert False, "expected ValueError"
        except ValueError as exc:
            assert str(exc) == "batch_size_exceeded"

    def test_invalid_url_raises(self) -> None:
        payload = {"url": "not a url", "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]}}
        try:
            asyncio.run(crawl_request(payload, http_fetcher=ok_fetcher,
                                       document_text_extractor=lambda b, c, u: "DOC",
                                       trafilatura_extractor=lambda h, u: "TRA", browser_worker=None))
            assert False, "expected ValueError"
        except ValueError as exc:
            assert str(exc).startswith("invalid_url")

    def test_success_with_injected_fetcher(self) -> None:
        payload = {"url": "http://example.com/", "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]}}
        result = asyncio.run(crawl_request(payload, http_fetcher=ok_fetcher,
                                           document_text_extractor=lambda b, c, u: "DOC",
                                           trafilatura_extractor=lambda h, u: "TRA", browser_worker=None))
        assert result["submitted_count"] == 1
        assert result["results"][0]["status"] == "success"

class TestServiceEdgeCases:
    def test_form_collector_select_textarea_button(self) -> None:
        forms = _extract_forms(
            "<form action='/submit' method='post'>"
            "<input type='text' name='q' value='a'>"
            "<select name='s'><option value='1'>x</option></select>"
            "<textarea name='t'>hi</textarea>"
            "<button name='b' value='go'>Go</button>"
            "</form>",
            "http://x/",
        )
        assert len(forms) == 1
        tags = {c["tag"] for c in forms[0]["controls"]}
        assert {"input", "select", "textarea", "button"} <= tags

    def test_extract_discovered_links_skips_ftp_scheme(self) -> None:
        links, count, truncated = _extract_discovered_links(
            "<a href='ftp://host/data'>x</a><a href='https://ok/x'>ok</a>", "http://x/"
        )
        assert all("ftp://" not in getattr(link, "url", str(link)) for link in links)
        assert any("https://ok/x" in getattr(link, "url", str(link)) for link in links)

    def test_should_join_document_paragraph_heading(self) -> None:
        # A heading line must never be joined to the next paragraph.
        assert _should_join_document_paragraph("CHAPTER ONE", "some prose") is False

    def test_collect_document_item_lines_breaks_on_public_participation(self) -> None:
        # A bare numbered marker followed by a non-item section breaks collection.
        title_lines, next_index = _collect_document_item_lines(
            ["1.", "Public Participation", "more"], 0
        )
        assert title_lines == []

    def test_looks_like_binary_body_magic_without_zip_extension(self) -> None:
        # PK magic with a non-.zip URL still classifies as binary.
        assert _looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", "http://x/file") is True

    def test_discovery_robots_success_and_malformed_crawl_delay(self) -> None:
        def fetcher(url, headers, timeout):
            if url.endswith("/robots.txt"):
                return make_fetch(body=b"User-agent: *\nCrawl-delay: notanumber\n")
            if "sitemap" in url:
                raise ConnectionError("no sitemap")
            return make_fetch(
                body=b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>hi</body></html>'
            )

        result = TestHttpStrategyResult()._run(
            fetcher=fetcher, policy={"discovery": {"robots": True, "sitemaps": True, "feeds": True}}
        )
        assert result.status == "success"
        assert "robots" in result.diagnostics
        assert result.diagnostics["robots"].get("crawl_delay_seconds") is None
        assert "discovery" in result.diagnostics
        assert result.diagnostics["discovery"]["feeds"]

class TestCallWorkerContract:
    def test_awaits_coroutine_worker_directly(self) -> None:
        class _Job:
            pass

        async def coro_worker(job):
            return "coro-result"

        assert asyncio.run(_call_worker(coro_worker, _Job())) == "coro-result"

    def test_runs_sync_worker_in_thread(self) -> None:
        class _Job:
            pass

        def sync_worker(job):
            return "sync-result"

        assert asyncio.run(_call_worker(sync_worker, _Job())) == "sync-result"

