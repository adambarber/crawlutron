"""Validation, headers, retries and parsing stay pure and total."""

import asyncio
from typing import Any

import pytest

from app.admission.payload_helpers import checked_payload_urls, payload_policy
from app.admission.policy import PolicyTier, read_policy
from app.api.render import render_options_for
from app.config.settings import CrawlerSettings
from app.extraction.document_text import format_extracted_document_text
from app.extraction.trees import FrameCollector, frames_from, walkable
from app.safety.validation import HostPolicy
from app.transport.fetch_policy import (
    fetch_with_retries,
    looks_like_binary_body,
    retry_after_seconds,
    retry_backoff_seconds,
)
from app.transport.http import http_strategy_result
from app.transport.request_profile import cookie_header_for_url, headers_for_strategy
from app.vocabulary.extraction import (
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_TRAFILATURA,
)
from app.vocabulary.strategies import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER
from tests._assertions import assert_result_failed, assert_result_success
from tests._extraction_helpers import extract_discovered_links
from tests._fetcher_factories import (
    make_fetch,
    ok_fetcher,
    raise_fetcher,
    routed_fetcher,
    run_chain,
    scripted_fetcher,
    strategy_request,
)
from tests._policy_factories import canonical, make_base_policy


class TestValidationHelpers:
    def test_checked_payload_urls_errors(self) -> None:
        from app.models.crawl_request import CrawlRequest

        assert checked_payload_urls(
            CrawlRequest(url="http://x/"),
            10,
            host_policy=HostPolicy.unrestricted(),
        ) == ["http://x/"]
        assert checked_payload_urls(
            CrawlRequest(urls=["http://x/", "http://y/"]),
            10,
            host_policy=HostPolicy.unrestricted(),
        ) == ["http://x/", "http://y/"]

    def test_checked_payload_urls_dedupes_exact_duplicates(self) -> None:
        from app.models.crawl_request import CrawlRequest

        assert checked_payload_urls(
            CrawlRequest(urls=["http://x/", "http://x/", "http://y/", "http://x/"]),
            10,
            host_policy=HostPolicy.unrestricted(),
        ) == ["http://x/", "http://y/"]

    def test_checked_payload_urls_dedupes_on_admitted_identity(self) -> None:
        from app.models.crawl_request import CrawlRequest

        # ``NormalizedUrl`` admission already treats fragment / trailing-slash / tracking-param / param-order
        # variants as the SAME page.
        urls = [
            "https://a.gov/x",
            "https://a.gov/x/#section",  # fragment dropped -> same page
            "https://a.gov/x/?utm_source=tw",  # tracking param dropped -> same page
            "https://a.gov/x/",  # trailing slash stripped -> same page
            "https://a.gov/y?a=1&b=2",
            "https://a.gov/y?b=2&a=1",  # param order only -> same page
            "https://a.gov/z",
        ]
        assert checked_payload_urls(
            CrawlRequest(urls=urls),
            10,
            host_policy=HostPolicy.unrestricted(),
        ) == [
            "https://a.gov/x",
            "https://a.gov/y?a=1&b=2",
            "https://a.gov/z",
        ]

    def test_render_options_error(self) -> None:
        with pytest.raises(ValueError, match=r"^invalid_options$"):
            render_options_for({"options": "bad"}, CrawlerSettings.from_mapping())

    def test_payload_policy_errors(self) -> None:
        tier = PolicyTier.model_defaults()
        cases: list[tuple[dict[str, Any], str]] = [
            ({"policy": "x"}, "invalid_policy"),
            ({"policy": {"limits": "x"}}, "invalid_policy_limits"),
            ({"policy": {"headers": "x"}}, "invalid_policy_headers"),
            ({"policy": {"strategies": "x"}}, "invalid_policy_strategies"),
            ({"policy": {"request_profile": "x"}}, "invalid_policy_profile"),
            ({"policy": {"extraction": "bogus"}}, "invalid_policy_extraction"),
        ]
        for payload, expected in cases:
            with pytest.raises(ValueError, match=rf"^{expected}$"):
                payload_policy(payload, tier)

    def test_retry_after_seconds_invalid(self) -> None:
        assert retry_after_seconds({"retry-after": "notanint"}) is None
        assert retry_after_seconds({"retry-after": "30"}) == 30
        assert retry_after_seconds({}) is None


class TestHeaderHelpers:
    def test_mobile_safari_user_agent(self) -> None:
        policy = {"request_profile": {"user_agent_family": "mobile_safari"}, "headers": {}}
        headers = headers_for_strategy(HTTP_DECLARED_CRAWLER, read_policy(policy), canonical("http://x/"))
        assert "iPhone" in headers["User-Agent"]

    def test_configured_user_agent_overrides_identity(self, monkeypatch) -> None:
        monkeypatch.setenv("CRAWLUTRON_USER_AGENT", "crawler-test/1")
        headers = headers_for_strategy(
            HTTP_DECLARED_CRAWLER, read_policy({"headers": {}}), canonical("http://x/")
        )
        assert headers["User-Agent"] == "crawler-test/1"

    def test_cookie_header_invalid_structures(self) -> None:
        assert (
            cookie_header_for_url({"cookie_jar": "per_host", "cookies": "notdict"}, canonical("http://host/"))
            == ""
        )
        assert (
            cookie_header_for_url(
                {"cookie_jar": "per_host", "cookies": {"host": "notdict"}}, canonical("http://host/")
            )
            == ""
        )


class TestFetchRetries:
    async def test_fetch_with_retries_reraises_after_exhaustion(self) -> None:
        with pytest.raises(ConnectionError):
            await fetch_with_retries(raise_fetcher, "http://x/", {}, 5, 0)

    def test_retry_backoff_is_exponential_and_capped(self) -> None:
        """Each retry gap doubles from the base and plateaus at the ceiling."""
        assert retry_backoff_seconds(1) == pytest.approx(0.25)
        assert retry_backoff_seconds(2) == pytest.approx(0.5)
        assert retry_backoff_seconds(3) == pytest.approx(1.0)
        # Deep retry budgets never exceed the ceiling: the gap plateaus.
        assert retry_backoff_seconds(20) == retry_backoff_seconds(8) == retry_backoff_seconds(4)

    async def test_retries_wait_between_attempts(self, monkeypatch) -> None:
        """A failing fetch is retried after a bounded backoff gap, not hammered."""
        sleeps: list[float] = []

        async def _rec_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(asyncio, "sleep", _rec_sleep)

        state = {"n": 0}
        answer = make_fetch(status=200, body=b"ok", final_url="http://x/")

        async def _flaky(url, headers, timeout_seconds, max_content_length=10_485_760):
            state["n"] += 1
            if state["n"] < 3:
                raise ConnectionError("transient")
            return answer

        response, meta = await fetch_with_retries(_flaky, "http://x/", {}, 5, 2)
        assert response is answer
        assert meta["attempts"] == 3
        # Gaps happen only between attempts (2 retries -> 2 waits), increasing and within the ceiling.
        assert sleeps == pytest.approx([0.25, 0.5])


class TestPureParsing:
    def test_range_action_expands_across_letters(self) -> None:
        # "1a - 1c" covers three items, and every one of them is stamped with the action. The observable claim
        # is per-item: each record carries the suffix.
        out = format_extracted_document_text(
            "1a - 1c Approve\n1a.\nAlpha\n1b.\nBeta\n1c.\nGamma",
            source_url="http://x/",
        )
        assert out == (
            "1a. Alpha [Action: Approve]\n\n1b. Beta [Action: Approve]\n\n1c. Gamma [Action: Approve]"
        )

    def test_range_with_mismatched_item_numbers_is_honoured_literally(self) -> None:
        # "1a - 5a" is a typo in the source document rather than a range: the two halves are stamped and
        # nothing in between is invented.
        out = format_extracted_document_text(
            "1a - 5a Approve\n1a.\nAlpha\n2a.\nBravo\n5a.\nDelta",
            source_url="http://x/",
        )
        assert out == "1a. Alpha [Action: Approve]\n\n2a. Bravo\n\n5a. Delta [Action: Approve]"

    def test_looks_like_binary_body_branches(self) -> None:
        assert (
            looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", canonical("http://x/z.zip"))
            is True
        )
        assert looks_like_binary_body(b"PK\x03\x04data", "text/html", canonical("http://x/")) is False
        assert looks_like_binary_body(b"data", "application/json", canonical("http://x/")) is False
        assert looks_like_binary_body(b"data", "application/octet-stream", canonical("http://x/")) is True
        assert looks_like_binary_body(b"data", "application/pdf", canonical("http://x/a.pdf")) is False

    def test_format_extracted_document_text_range_action(self) -> None:
        text = "1a - 1c Approve\n1a.\nBuild the thing\n1b.\nFix the bug\n1c.\nShip it"
        out = format_extracted_document_text(text, source_url="http://x/")
        assert "Approve" in out

    def test_should_join_document_paragraph_heading(self) -> None:
        # Kept as the negative half of the heading rule from the pure-parsing side. The heading and the prose
        # stay two records, so the heading did not absorb what followed it.
        out = format_extracted_document_text("Section A\nsome prose", source_url="http://x/")
        assert out == "Section A\n\nsome prose"

    def test_extract_frames_skips_bad_and_dupes(self) -> None:
        html = (
            '<iframe src="http://same.example/f"></iframe>'
            '<iframe src="javascript:alert(1)"></iframe>'
            '<iframe src="http://same.example/f"></iframe>'
        )
        walked = walkable(
            html,
            canonical("http://same.example/"),
            None,
        )
        assert walked is not None
        frames = frames_from(walked, canonical("http://same.example/"))
        assert len(frames) == 1

    def test_extract_discovered_links_skips_bad_scheme(self) -> None:
        html = '<a href="mailto:a@b.com">x</a><a href="http://other.example/p">y</a>'
        links, count, truncated = extract_discovered_links(html, canonical("http://example.com/"))
        assert len(links) == 1

    def test_frame_collector_ignores_frames_without_src(self) -> None:
        collector = FrameCollector(canonical("http://x/"))
        collector.feed('<frame></frame><iframe src="http://x/f"></iframe>')
        assert len(collector.frames) == 1


# The three discovery probes turned on together, as one named policy fragment.
DISCOVERY_ALL_ON = {"robots": True, "sitemaps": True, "feeds": True}


# The page every discovery test below is fetched from, advertising one feed.
FEED_ADVERTISING_PAGE = (
    200,
    b'<html><body><link rel="alternate" type="application/rss+xml"'
    b' href="http://example.com/feed.xml"></body></html>',
    "text/html",
)


# The URL the default single-page drive fetches, and therefore the one a caller has to differ from for
# ``final_url`` to mean anything.
_PAGE_URL = "http://example.com/"


async def _run_http_result(
    status=200,
    strategy=HTTP_DECLARED_CRAWLER,
    fetcher=None,
    ctype="text/html; charset=utf-8",
    body=b"<html><body>hello</body></html>",
    final_url=_PAGE_URL,
    policy=None,
):
    """Drive the real ``http_strategy_result`` over a default single-page policy.

    Module-level so the discovery tests share it, not another class's harness;
    ``final_url`` is honoured now -- it used to be accepted and dropped.
    """
    if fetcher is None:
        fetcher = (
            scripted_fetcher(status=status, ctype=ctype, body=body)
            if final_url == _PAGE_URL
            else routed_fetcher(
                default=make_fetch(status=status, body=body, ctype=ctype, final_url=final_url),
            )
        )

    # A policy partial merges as a raw dict and is read once, the way the wire merges it.
    base = make_base_policy()
    pol = read_policy({**base.model_dump(), **policy}) if policy else base
    return await http_strategy_result(
        request=strategy_request("http://example.com/", pol, strategy),
        http_fetcher=fetcher,
    )


async def _diagnostics_for_failed_probes(fetcher, **policy):
    """Full discovery whose probes fail: the ``(result, diagnostics)`` pair.

    Discovery probes fail; the page fetch does not. The point is a failed probe
    is recorded as diagnostics and does not fail the crawl.
    """
    result = await _run_http_result(
        fetcher=fetcher,
        policy={
            "limits": {"timeout_seconds": 5, "max_retries": 0},
            "discovery": DISCOVERY_ALL_ON,
            **policy,
        },
    )
    assert result.status == "success"
    assert "robots" in result.diagnostics
    return result, result.diagnostics


class TestHttpStrategyResult:
    _run = staticmethod(_run_http_result)

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
        # A raw ConnectionError classifies specifically as connection_error, not the generic network_error
        # tail: the old broad-except sink hardcoded network_error and masked the real code; the shared
        assert_result_failed(await self._run(fetcher=raise_fetcher), "connection_error")

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
        feed_fetcher = routed_fetcher(
            {
                "/robots.txt": ConnectionError("discovery failed"),
                "/sitemap.xml": ConnectionError("discovery failed"),
            },
            default=FEED_ADVERTISING_PAGE,
        )

        _result, diagnostics = await _diagnostics_for_failed_probes(feed_fetcher)
        assert diagnostics["robots"].get("error") is not None
        # The feed the page advertises is still discovered: a failed robots or sitemap probe costs a
        # diagnostic, not the discovery pass.
        assert diagnostics["discovery"]["feeds"] is not None


class TestExecuteCrawlChain:
    async def test_http_strategy_runs(self) -> None:
        assert_result_success(
            await run_chain(
                fetcher=ok_fetcher, policy=dict(make_base_policy(), strategies=[HTTP_DECLARED_CRAWLER])
            )
        )

    async def test_browser_strategy_without_worker(self) -> None:
        assert (
            await run_chain(
                fetcher=ok_fetcher, policy=dict(make_base_policy(), strategies=[BROWSER_HEADLESS])
            )
        ).classification == "browser_crash"

    async def test_unsupported_strategy(self) -> None:
        # A strategy outside CrawlStrategyName is rejected at construction of the attempt: the strategy axis
        with pytest.raises(ValueError, match=r"\.strategy="):
            await run_chain(
                fetcher=ok_fetcher,
                policy=make_base_policy().model_copy(update={"strategies": ["NOT_A_STRATEGY"]}),
            )
