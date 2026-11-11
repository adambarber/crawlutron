"""Validation, headers, retries and parsing stay pure and total."""

import asyncio
import datetime
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from crawlutron.admission.payload_helpers import checked_payload_urls
from crawlutron.admission.policy import PolicyTier, read_policy
from crawlutron.config.settings import CrawlerSettings
from crawlutron.extraction.document_text import format_extracted_document_text
from crawlutron.extraction.trees import FrameCollector, frames_from, walkable
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.json_value import JsonValue
from crawlutron.models.render_request import RenderRequest
from crawlutron.service.render import render_options_for
from crawlutron.transport.fetch_policy import (
    fetch_with_retries,
    looks_like_binary_body,
    retry_after_seconds,
    retry_backoff_seconds,
)
from crawlutron.transport.request_profile import cookie_header_for_url, headers_for_strategy
from crawlutron.vocabulary.extraction import (
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_TRAFILATURA,
)
from crawlutron.vocabulary.strategies import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER
from tests._assertions import assert_result_failed, assert_result_success
from tests._extraction_helpers import extract_discovered_links
from tests._fetch_doubles import FetcherDouble, make_fetch
from tests._fetcher_factories import diagnostics_for_failed_probes as _diagnostics_for_failed_probes
from tests._fetcher_factories import run_chain
from tests._fetcher_factories import run_http_result as _run_http_result
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical, make_base_policy


def _raise_fetcher(url, *_):
    raise ConnectionError("network down")


def _ok_fetcher(url, *_):
    return make_fetch(url=url)


class TestValidationHelpers:
    def test_checked_payload_urls_errors(self) -> None:
        pairs = checked_payload_urls(CrawlRequest(url="http://x/"), 10, host_policy=UNRESTRICTED_HOST_POLICY)
        assert [text for text, _ in pairs] == ["http://x/"]

    @pytest.mark.parametrize(
        ("raw_urls", "expected"),
        [
            (["http://x/", "http://y/"], ["http://x/", "http://y/"]),
            (["http://x/", "http://x/", "http://y/", "http://x/"], ["http://x/", "http://y/"]),
        ],
        ids=["batch", "dedupes_exact_duplicates"],
    )
    def test_checked_payload_urls_batch_url_field(self, raw_urls: list[str], expected: list[str]) -> None:
        pairs = checked_payload_urls(
            CrawlRequest(urls=raw_urls),
            10,
            host_policy=UNRESTRICTED_HOST_POLICY,
        )
        assert [text for text, _ in pairs] == expected

    def test_checked_payload_urls_dedupes_on_admitted_identity(self) -> None:
        # ``NormalizedUrl`` admission treats fragment / tracking-param / param-order
        # variants as the SAME page; #520 keeps path text verbatim, so /x/ stays distinct.
        urls = [
            "https://a.gov/x",
            "https://a.gov/x/#section",  # fragment dropped -> the /x/ page, not /x
            "https://a.gov/x/?utm_source=tw",  # tracking param dropped -> same /x/ page
            "https://a.gov/x/",  # same page again
            "https://a.gov/y?a=1&b=2",
            "https://a.gov/y?b=2&a=1",  # param order only -> same page
            "https://a.gov/z",
        ]
        pairs = checked_payload_urls(CrawlRequest(urls=urls), 10, host_policy=UNRESTRICTED_HOST_POLICY)
        assert [text for text, _ in pairs] == [
            "https://a.gov/x",
            "https://a.gov/x/#section",
            "https://a.gov/y?a=1&b=2",
            "https://a.gov/z",
        ]

    def test_render_options_error(self) -> None:
        with pytest.raises(ValidationError):
            render_options_for(
                RenderRequest.model_validate({"url": "http://x/", "options": "bad"}),
                CrawlerSettings.from_mapping(),
            )

    def test_payload_policy_errors(self) -> None:
        from crawlutron.admission.policy import validate_wire_policy

        tier = PolicyTier.model_defaults()
        # The wire door must reject non-mapping and bad-shape policies; a raw
        # object callable is the honest spelling of "deliberately wrong shape".
        bad_wire: Callable[..., object] = validate_wire_policy
        cases: list[tuple[object, str]] = [
            ("x", "invalid_policy"),
            ({"limits": "x"}, "invalid_policy_limits"),
            ({"headers": "x"}, "invalid_policy_headers"),
            ({"strategies": "x"}, "invalid_policy_strategies"),
            ({"request_profile": "x"}, "invalid_policy_profile"),
            ({"extraction": "bogus"}, "invalid_policy_extraction"),
        ]
        for policy, expected in cases:
            with pytest.raises(ValueError, match=rf"^{expected}$"):
                bad_wire(policy, tier)

    def test_retry_after_seconds_invalid(self) -> None:
        assert retry_after_seconds({"retry-after": "notanint"}) is None
        assert retry_after_seconds({"retry-after": "30"}) == 30
        assert retry_after_seconds({}) is None

    def test_retry_after_seconds_http_date_yields_a_cooldown(self) -> None:
        """#581: the HTTP-date form must produce a politeness cooldown, not be dropped.

        ``int(value)`` silently discarded it (ValueError -> None), so a 503 that
        answers 'retry at <date>' got zero cooldown and was hammered.
        """
        future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=60)).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        cooldown = retry_after_seconds({"retry-after": future})
        assert cooldown is not None and 0 < cooldown <= 300, (
            f"an HTTP-date Retry-After produced {cooldown!r}: dropped, not converted"
        )

    def test_retry_after_seconds_past_http_date_clamps_to_zero(self) -> None:
        past = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=60)).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
        assert retry_after_seconds({"retry-after": past}) == 0


class TestHeaderHelpers:
    def test_mobile_safari_user_agent(self) -> None:
        policy: dict[str, JsonValue] = {
            "request_profile": {"user_agent_family": "mobile_safari"},
            "headers": {},
        }
        headers = headers_for_strategy(HTTP_DECLARED_CRAWLER, read_policy(policy), canonical("http://x/"))
        assert "iPhone" in headers["User-Agent"]

    def test_configured_user_agent_overrides_identity(self, monkeypatch) -> None:
        monkeypatch.setenv("CRAWLUTRON_USER_AGENT", "crawler-test/1")
        headers = headers_for_strategy(
            HTTP_DECLARED_CRAWLER,
            read_policy({"headers": {}}),
            canonical("http://x/"),
        )
        assert headers["User-Agent"] == "crawler-test/1"

    def test_cookie_header_invalid_structures(self) -> None:
        # A non-dict cookie jar value is rejected. object is the honest spelling of
        # "deliberately wrong value type"; the door asserts it does not raise.
        bad_profile: Callable[..., object] = cookie_header_for_url
        invalid: object = {"cookie_jar": "per_host", "cookies": "notdict"}
        assert bad_profile(invalid, canonical("http://host/")) == ""
        assert (
            cookie_header_for_url(
                {"cookie_jar": "per_host", "cookies": {"host": "notdict"}}, canonical("http://host/")
            )
            == ""
        )


class TestFetchRetries:
    async def test_fetch_with_retries_reraises_after_exhaustion(self) -> None:
        with pytest.raises(ConnectionError):
            await fetch_with_retries(_raise_fetcher, canonical("http://x/"), {}, 5, 0)

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

        response, meta = await fetch_with_retries(_flaky, canonical("http://x/"), {}, 5, 2)
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


class TestHttpStrategyResult:
    _run = staticmethod(_run_http_result)

    @pytest.fixture(autouse=True)
    def _isolated_doc_pool(self, monkeypatch):
        """A fresh isolated doc pool, immune to a prior test's terminal teardown (#246)."""
        from tests._doc_pool_fixtures import fresh_doc_pool

        fresh_doc_pool(monkeypatch)

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
        assert_result_failed(await self._run(fetcher=_raise_fetcher), "connection_error")

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
        _failed_probe = ConnectionError("discovery failed")
        feed_fetcher = FetcherDouble(
            lambda url, _sent_headers, *_: (
                _failed_probe
                if any(f in url.raw for f in ("/robots.txt", "/sitemap.xml"))
                else FEED_ADVERTISING_PAGE
            )
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
                fetcher=_ok_fetcher, policy=dict(make_base_policy(), strategies=[HTTP_DECLARED_CRAWLER])
            )
        )

    async def test_browser_strategy_without_worker(self) -> None:
        assert (
            await run_chain(
                fetcher=_ok_fetcher, policy=dict(make_base_policy(), strategies=[BROWSER_HEADLESS])
            )
        ).classification == "browser_crash"

    async def test_unsupported_strategy(self) -> None:
        # A strategy outside CrawlStrategyName is rejected at construction of the attempt: the strategy axis
        with pytest.raises(ValueError, match=r"\.strategy="):
            await run_chain(
                fetcher=_ok_fetcher,
                policy=make_base_policy().model_copy(update={"strategies": ["NOT_A_STRATEGY"]}),
            )
