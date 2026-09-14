"""Real tests for service.py logic (no mocks; inject fakes for network)."""

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault(
    "BROWSER_CRAWLER_LOG_FILE", str(Path(tempfile.gettempdir()) / "browser-crawler-test.jsonl")
)

from _testkit import (
    assert_result_failed,
    assert_result_success,
    make_base_policy,
    make_fetch,
    ok_fetcher,
    raise_fetcher,
)

from app.api.render import _render_options
from app.constants import (
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_TRAFILATURA,
    HTTP_DECLARED_CRAWLER,
)
from app.crawl.document_text import (
    _collect_document_item_lines,
    _document_item_range,
    _format_extracted_document_text,
    _next_non_empty_document_line,
    _should_join_document_paragraph,
)
from app.crawl.engine import (
    _call_worker,
    _execute_crawl_chain,
    _payload_policy,
    _payload_urls,
    crawl_request,
)
from app.crawl.extraction import (
    _extract_discovered_links,
    _extract_forms,
    _extract_frames,
    _FrameCollector,
)
from app.crawl.strategies.http import (
    _cookie_header_for_url,
    _fetch_with_retries,
    _headers_for_strategy,
    _http_strategy_result,
    _looks_like_binary_body,
    _retry_after_seconds,
    _select_fingerprint,
)
from app.settings import CrawlerSettings


def discovery_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
    if url.endswith("/robots.txt") or url.endswith("/sitemap.xml"):
        raise ConnectionError("discovery failed")
    return make_fetch()


class TestValidationHelpers:
    def test_payload_urls_errors(self) -> None:
        with pytest.raises(ValueError, match=r"^invalid_urls$"):
            _payload_urls({"urls": "notalist"})
        with pytest.raises(ValueError, match=r"^missing_url$"):
            _payload_urls({})
        assert _payload_urls({"url": "http://x/"}) == ["http://x/"]
        assert _payload_urls({"urls": ["http://x/", "http://y/"]}) == ["http://x/", "http://y/"]

    def test_payload_urls_dedupes_exact_duplicates(self) -> None:
        assert _payload_urls({"urls": ["http://x/", "http://x/", "http://y/", "http://x/"]}) == [
            "http://x/",
            "http://y/",
        ]

    def test_render_options_error(self) -> None:
        with pytest.raises(ValueError, match=r"^invalid_options$"):
            _render_options({"options": "bad"}, CrawlerSettings.from_mapping())

    def test_payload_policy_errors(self) -> None:
        s = CrawlerSettings.from_mapping()
        cases = [
            ({"policy": "x"}, "invalid_policy"),
            ({"policy": {"limits": "x"}}, "invalid_policy_limits"),
            ({"policy": {"headers": "x"}}, "invalid_policy_headers"),
            ({"policy": {"strategies": "x"}}, "invalid_policy_strategies"),
            ({"policy": {"request_profile": "x"}}, "invalid_policy_profile"),
            ({"policy": {"extraction": "bogus"}}, "invalid_policy_extraction"),
        ]
        for payload, expected in cases:
            with pytest.raises(ValueError, match=rf"^{expected}$"):
                _payload_policy(payload, s)

    def test_retry_after_seconds_invalid(self) -> None:
        assert _retry_after_seconds({"retry-after": "notanint"}) is None
        assert _retry_after_seconds({"retry-after": "30"}) == 30
        assert _retry_after_seconds({}) is None


class TestHeaderHelpers:
    def test_mobile_safari_user_agent(self) -> None:
        policy = {"request_profile": {"user_agent_family": "mobile_safari"}, "headers": {}}
        headers = _headers_for_strategy(HTTP_DECLARED_CRAWLER, policy, "http://x/")
        assert "iPhone" in headers["User-Agent"]

    def test_cookie_header_invalid_structures(self) -> None:
        assert _cookie_header_for_url({"cookie_jar": "per_host", "cookies": "notdict"}, "http://host/") == ""
        assert (
            _cookie_header_for_url({"cookie_jar": "per_host", "cookies": {"host": "notdict"}}, "http://host/")
            == ""
        )


class TestFetchRetries:
    async def test_fetch_with_retries_reraises_after_exhaustion(self) -> None:
        with pytest.raises(ConnectionError):
            await _fetch_with_retries(raise_fetcher, "http://x/", {}, 5, 0)


class TestPureParsing:
    def test_document_item_range_branches(self) -> None:
        assert _document_item_range("1a", "5a") == ["1a", "5a"]
        assert _document_item_range("1a", "1e") == ["1a", "1b", "1c", "1d", "1e"]
        assert _document_item_range("1a", "5b") == ["1a", "5b"]
        assert _document_item_range("1a", "x") == ["1a", "x"]

    def test_looks_like_binary_body_branches(self) -> None:
        assert (
            _looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", "http://x/z.zip") is True
        )
        assert _looks_like_binary_body(b"PK\x03\x04data", "text/html", "http://x/") is False
        assert _looks_like_binary_body(b"data", "application/json", "http://x/") is False
        assert _looks_like_binary_body(b"data", "application/octet-stream", "http://x/") is True
        assert _looks_like_binary_body(b"data", "application/pdf", "http://x/a.pdf") is False

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
    async def _run(
        self,
        status=200,
        strategy=HTTP_DECLARED_CRAWLER,
        fetcher=None,
        ctype="text/html; charset=utf-8",
        body=b"<html><body>hello</body></html>",
        final_url="http://example.com/",
        policy=None,
    ):
        if fetcher is None:

            def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
                return make_fetch(status=status, ctype=ctype, body=body, final_url=final_url, url=url)

        pol = make_base_policy()
        if policy:
            pol.update(policy)
        return await _http_strategy_result(
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            strategy=strategy,
            policy=pol,
            http_fetcher=fetcher,
        )

    async def test_success_html(self) -> None:
        assert_result_success(await self._run())

    async def test_status_classification_overrides(self) -> None:
        for status, classification in [
            (403, "access_denied"),
            (404, "not_found"),
            (410, "gone"),
            (429, "rate_limited"),
            (500, "site_error"),
        ]:
            assert (await self._run(status=status)).classification == classification, status

    async def test_network_error_handler(self) -> None:
        assert_result_failed(await self._run(fetcher=raise_fetcher), "network_error")

    async def test_binary_body_short_circuits(self) -> None:
        result = await self._run(
            ctype="application/octet-stream",
            body=b"PK\x03\x04data",
            final_url="http://example.com/a.zip",
        )
        assert result.classification == EXTRACTION_METHOD_BINARY_NO_TEXT

    async def test_document_extract_strategy(self) -> None:
        result = await self._run(
            policy={"extraction": EXTRACTION_DOCUMENT},
            ctype="text/csv",
            body=b"name,age\nAlice,30\nBob,25",
        )
        assert "Alice" in (result.text or "")

    async def test_trafilatura_extract_html_and_non_html(self) -> None:
        assert "hello" in ((await self._run(policy={"extraction": EXTRACTION_TRAFILATURA})).text or "")
        non_html = await self._run(
            policy={"extraction": EXTRACTION_TRAFILATURA}, ctype="application/json", body=b"{}"
        )
        assert non_html.text == ""

    async def test_discovery_diagnostics_records_exceptions_and_feeds(self) -> None:
        def feed_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            if url.endswith("/robots.txt") or url.endswith("/sitemap.xml"):
                raise ConnectionError("discovery failed")
            return make_fetch(
                body=b'<html><body><link rel="alternate" href="http://example.com/feed.xml"></body></html>'
            )

        result = await self._run(
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
            policy=dict(make_base_policy(), strategies=[strategy]),
            http_fetcher=ok_fetcher,
            cffi_fetcher=ok_fetcher,
            browser_worker=None,
            **kwargs,
        )

    async def test_http_strategy_runs(self) -> None:
        assert_result_success(await self._run_chain(HTTP_DECLARED_CRAWLER))

    async def test_browser_strategy_without_worker(self) -> None:
        assert (await self._run_chain(BROWSER_HEADLESS)).classification == "browser_crash"

    async def test_unsupported_strategy(self) -> None:
        assert (await self._run_chain("NOT_A_STRATEGY")).classification == "unknown_error"


class TestCrawlRequest:
    async def test_batch_size_exceeded(self) -> None:
        settings = CrawlerSettings.from_mapping()
        payload = {
            # Distinct URLs: the cap counts unique URLs (exact duplicates
            # collapse in _payload_urls before this check).
            "urls": [f"http://example.com/{i}" for i in range(settings.max_batch_size + 1)],
            "policy": {"strategies": [HTTP_DECLARED_CRAWLER]},
        }
        with pytest.raises(ValueError, match=r"^batch_size_exceeded$"):
            await crawl_request(payload, settings=settings, http_fetcher=ok_fetcher, browser_worker=None)

    async def test_invalid_url_raises(self) -> None:
        payload = {"url": "not a url", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}}
        with pytest.raises(ValueError, match=r"^invalid_url"):
            await crawl_request(payload, http_fetcher=ok_fetcher, browser_worker=None)

    async def test_success_with_injected_fetcher(self) -> None:
        payload = {"url": "http://example.com/", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}}
        result = await crawl_request(payload, http_fetcher=ok_fetcher, browser_worker=None)
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

    def test_extract_discovered_links_handles_unnormalizable_source_url(self) -> None:
        # normalize_public_http_url rejects non-http source URLs; the link extractor
        # must fall back to the raw source rather than crash.
        links, count, truncated = _extract_discovered_links(
            "<a href='https://ok.example/x'>ok</a>", "ftp://example.com/"
        )
        assert count == 1
        assert truncated is False

    def test_should_join_document_paragraph_heading(self) -> None:
        # A heading line must never be joined to the next paragraph.
        assert _should_join_document_paragraph("CHAPTER ONE", "some prose") is False

    def test_collect_document_item_lines_breaks_on_public_participation(self) -> None:
        # A bare numbered marker followed by a non-item section breaks collection.
        title_lines, next_index = _collect_document_item_lines(["1.", "Public Participation", "more"], 0)
        assert title_lines == []

    def test_looks_like_binary_body_magic_without_zip_extension(self) -> None:
        # PK magic with a non-.zip URL still classifies as binary.
        assert _looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", "http://x/file") is True

    async def test_discovery_robots_success_and_malformed_crawl_delay(self) -> None:
        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            if url.endswith("/robots.txt"):
                return make_fetch(body=b"User-agent: *\nCrawl-delay: notanumber\n")
            if "sitemap" in url:
                raise ConnectionError("no sitemap")
            return make_fetch(
                body=b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>hi</body></html>'
            )

        result = await TestHttpStrategyResult()._run(
            fetcher=fetcher,
            policy={
                "limits": {"timeout_seconds": 5, "max_retries": 0},
                "discovery": {"robots": True, "sitemaps": True, "feeds": True},
            },
        )
        assert result.status == "success"
        assert "robots" in result.diagnostics
        assert result.diagnostics["robots"].get("crawl_delay_seconds") is None
        assert "discovery" in result.diagnostics
        assert result.diagnostics["discovery"]["feeds"]


class TestCallWorkerContract:
    async def test_awaits_coroutine_worker_directly(self) -> None:
        class _Job:
            pass

        async def coro_worker(job):
            return "coro-result"

        assert await _call_worker(coro_worker, _Job()) == "coro-result"

    async def test_runs_sync_worker_in_thread(self) -> None:
        class _Job:
            pass

        def sync_worker(job):
            return "sync-result"

        assert await _call_worker(sync_worker, _Job()) == "sync-result"


class TestFingerprintInjectionRotation:
    def test_user_agent_pool_rotates_per_domain_and_is_stable(self):
        profile = {"user_agents": ["UA-A", "UA-B", "UA-C"]}
        # Use more hosts than pool size so rotation is clearly visible.
        hosts = [f"{c}.com" for c in "abcdef"]
        seen = {d: _select_fingerprint(profile, d).get("user_agent") for d in hosts}
        # rotation: different hosts get a variety of UAs (not all identical)
        assert len(set(seen.values())) > 1
        # stability: same host -> same UA across calls (keeps content-cache key stable)
        assert _select_fingerprint(profile, "a.com")["user_agent"] == seen["a.com"]
        assert _select_fingerprint(profile, "a.com")["user_agent"] == seen["a.com"]

    def test_fingerprint_rotation_changes_accept_language_per_domain(self):
        base = {"request_profile": {"fingerprint_rotation": True}, "headers": {}}
        langs = {
            d: _headers_for_strategy("HTTP_DECLARED_CRAWLER", base, f"http://{d}/")["Accept-Language"]
            for d in ("a.com", "b.com", "c.com", "d.com", "e.com")
        }
        # across 5 distinct hosts the built-in pool (size 5) yields variety
        assert len(set(langs.values())) > 1
        # same host is stable
        again = _headers_for_strategy("HTTP_DECLARED_CRAWLER", base, "http://a.com/")["Accept-Language"]
        assert again == langs["a.com"]

    def test_default_request_has_no_fingerprint_rotation(self):
        headers = _headers_for_strategy(
            "HTTP_DECLARED_CRAWLER",
            {"request_profile": {}, "headers": {}},
            "http://x/",
        )
        assert headers["Accept-Language"] == "en-US,en;q=0.9"
        assert "Mozilla" not in headers["User-Agent"]

    def test_fingerprint_injected_user_agent_lands_in_headers(self):
        profile = {"request_profile": {"user_agents": ["Rotated-UA-1", "Rotated-UA-2"]}, "headers": {}}
        h1 = _headers_for_strategy("HTTP_DECLARED_CRAWLER", profile, "http://a.com/")
        h2 = _headers_for_strategy("HTTP_DECLARED_CRAWLER", profile, "http://b.com/")
        assert h1["User-Agent"] == "Rotated-UA-1"
        assert h2["User-Agent"] == "Rotated-UA-2"

    def test_explicit_headers_still_override_fingerprint(self):
        profile = {"request_profile": {"user_agents": ["Rotated-UA-1"]}, "headers": {"User-Agent": "forced"}}
        h = _headers_for_strategy("HTTP_DECLARED_CRAWLER", profile, "http://a.com/")
        assert h["User-Agent"] == "forced"
