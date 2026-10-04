"""Fetch and service helpers: headers, cookie handling, quality scoring, decode and rendering."""

import asyncio
import gzip
from typing import Any

import brotli
import pytest

from app.api.render import _render_options
from app.constants import (
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_TRAFILATURA,
    HTTP_DECLARED_CRAWLER,
    status_for_classification,
)
from app.crawl.cache import HttpCache
from app.crawl.document_text import format_extracted_document_text
from app.crawl.engine import (
    _payload_policy,
    crawl_request,
)
from app.crawl.extraction import (
    _FrameCollector,
    _walkable,
    forms_from,
    frames_from,
)
from app.crawl.payload_helpers import _payload_urls
from app.crawl.policy import PolicyTier, read_policy
from app.crawl.strategies.fetch_policy import (
    fetch_with_retries,
    looks_like_binary_body,
    retry_after_seconds,
    retry_backoff_seconds,
)
from app.crawl.strategies.http import http_strategy_result
from app.crawl.strategies.request_profile import (
    _cookie_header_for_url,
    _select_fingerprint,
    headers_for_strategy,
)
from app.settings import CrawlerSettings
from app.validation import HostPolicy
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
from tests._http_test_helpers import (
    HTTP_CFFI_IMPERSONATE,
    decode_body,
    decompress_body,
    quality_score,
)
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, make_base_policy
from app.models.normalized_url import NormalizedUrl


def _url(text: str) -> NormalizedUrl:
    """Admission helper: every URL seam in this module takes the value."""
    return NormalizedUrl(text, policy=UNRESTRICTED_HOST_POLICY)
from tests._worker_doubles import (
    assert_coroutine_worker_is_awaited_directly,
    assert_sync_worker_runs_once_in_a_thread,
)


class TestValidationHelpers:
    def test_payload_urls_errors(self) -> None:
        with pytest.raises(ValueError, match=r"^invalid_urls$"):
            _payload_urls(
                {"urls": "notalist"},
                host_policy=HostPolicy.unrestricted(),
            )
        with pytest.raises(ValueError, match=r"^missing_url$"):
            _payload_urls({}, host_policy=HostPolicy.unrestricted())
        assert _payload_urls({"url": "http://x/"}, host_policy=HostPolicy.unrestricted()) == ["http://x/"]
        assert _payload_urls(
            {"urls": ["http://x/", "http://y/"]},
            host_policy=HostPolicy.unrestricted(),
        ) == ["http://x/", "http://y/"]

    def test_payload_urls_dedupes_exact_duplicates(self) -> None:
        assert _payload_urls(
            {"urls": ["http://x/", "http://x/", "http://y/", "http://x/"]},
            host_policy=HostPolicy.unrestricted(),
        ) == [
            "http://x/",
            "http://y/",
        ]

    def test_payload_urls_dedupes_by_canonical_page_identity(self) -> None:
        # ``NormalizedUrl`` admission already treats fragment / trailing-slash /
        # tracking-param / param-order variants as the SAME page. The batch chokepoint
        # must dedup on that identity, not the raw string -- otherwise a request
        # carrying both ``.../x`` and ``.../x/?utm_source=tw`` crawls one page twice.
        urls = [
            "https://a.gov/x",
            "https://a.gov/x/#section",  # fragment dropped -> same page
            "https://a.gov/x/?utm_source=tw",  # tracking param dropped -> same page
            "https://a.gov/x/",  # trailing slash stripped -> same page
            "https://a.gov/y?a=1&b=2",
            "https://a.gov/y?b=2&a=1",  # param order only -> same page
            "https://a.gov/z",
        ]
        assert _payload_urls(
            {"urls": urls},
            host_policy=HostPolicy.unrestricted(),
        ) == [
            "https://a.gov/x",
            "https://a.gov/y?a=1&b=2",
            "https://a.gov/z",
        ]

    def test_render_options_error(self) -> None:
        with pytest.raises(ValueError, match=r"^invalid_options$"):
            _render_options({"options": "bad"}, CrawlerSettings.from_mapping())

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
                _payload_policy(payload, tier)

    def test_retry_after_seconds_invalid(self) -> None:
        assert retry_after_seconds({"retry-after": "notanint"}) is None
        assert retry_after_seconds({"retry-after": "30"}) == 30
        assert retry_after_seconds({}) is None


class TestHeaderHelpers:
    def test_mobile_safari_user_agent(self) -> None:
        policy = {"request_profile": {"user_agent_family": "mobile_safari"}, "headers": {}}
        headers = headers_for_strategy(HTTP_DECLARED_CRAWLER, read_policy(policy), _url("http://x/"))
        assert "iPhone" in headers["User-Agent"]

    def test_configured_user_agent_overrides_identity(self, monkeypatch) -> None:
        monkeypatch.setenv("CRAWLUTRON_USER_AGENT", "crawler-test/1")
        headers = headers_for_strategy(HTTP_DECLARED_CRAWLER, read_policy({"headers": {}}), _url("http://x/"))
        assert headers["User-Agent"] == "crawler-test/1"

    def test_cookie_header_invalid_structures(self) -> None:
        assert _cookie_header_for_url({"cookie_jar": "per_host", "cookies": "notdict"}, _url("http://host/")) == ""
        assert (
            _cookie_header_for_url({"cookie_jar": "per_host", "cookies": {"host": "notdict"}}, _url("http://host/"))
            == ""
        )


class TestFetchRetries:
    async def test_fetch_with_retries_reraises_after_exhaustion(self) -> None:
        with pytest.raises(ConnectionError):
            await fetch_with_retries(raise_fetcher, "http://x/", {}, 5, 0)

    def test_retry_backoff_is_exponential_and_capped(self) -> None:
        """Each retry gap doubles from the base and plateaus at the ceiling."""
        from app.crawl.strategies.fetch_policy import _RETRY_BACKOFF_CAP_SECONDS

        assert retry_backoff_seconds(1) == pytest.approx(0.25)
        assert retry_backoff_seconds(2) == pytest.approx(0.5)
        assert retry_backoff_seconds(3) == pytest.approx(1.0)
        assert retry_backoff_seconds(4) == pytest.approx(_RETRY_BACKOFF_CAP_SECONDS)
        # Deep retry budgets never exceed the ceiling.
        assert retry_backoff_seconds(20) == pytest.approx(_RETRY_BACKOFF_CAP_SECONDS)

    async def test_retries_wait_between_attempts(self, monkeypatch) -> None:
        """A failing fetch is retried after a bounded backoff gap, not hammered."""
        sleeps: list[float] = []

        async def _rec_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(asyncio, "sleep", _rec_sleep)

        state = {"n": 0}
        answer = make_fetch(status=200, body=b"ok", final_url="http://x/")

        # ``max_content_length`` defaults, because ``HttpFetcher`` says a caller
        # may omit it: a double that required the fourth argument was not
        # substitutable for the fetcher this function is handed.
        async def _flaky(url, headers, timeout_seconds, max_content_length=10_485_760):
            state["n"] += 1
            if state["n"] < 3:
                raise ConnectionError("transient")
            return answer

        # A real ``HttpFetchResult``, not a bare string: this is handed to
        # ``fetch_with_retries`` as an ``HttpFetcher``, and a fetcher that
        # answered with something the contract does not allow proved the retry
        # loop passes its answer through -- which it does -- by asserting on a
        # shape production can never produce.
        response, meta = await fetch_with_retries(_flaky, "http://x/", {}, 5, 2)
        assert response is answer
        assert meta["attempts"] == 3
        # Gaps happen only between attempts (2 retries -> 2 waits), increasing
        # and within the ceiling.
        assert sleeps == pytest.approx([0.25, 0.5])


class TestPureParsing:
    def test_range_action_expands_across_letters(self) -> None:
        # "1a - 1c" covers three items, and every one of them is stamped with the
        # action. The observable claim is per-item: each record carries the suffix.
        out = format_extracted_document_text(
            "1a - 1c Approve\n1a.\nAlpha\n1b.\nBeta\n1c.\nGamma",
            source_url="http://x/",
        )
        assert out == (
            "1a. Alpha [Action: Approve]\n\n1b. Beta [Action: Approve]\n\n1c. Gamma [Action: Approve]"
        )

    def test_range_with_mismatched_item_numbers_is_honoured_literally(self) -> None:
        # "1a - 5a" is a typo in the source document rather than a range: the two
        # halves are stamped and nothing in between is invented. 2a sits between them
        # on purpose, so the test fails if a literal pair were expanded into 1a..5a.
        out = format_extracted_document_text(
            "1a - 5a Approve\n1a.\nAlpha\n2a.\nBravo\n5a.\nDelta",
            source_url="http://x/",
        )
        assert out == "1a. Alpha [Action: Approve]\n\n2a. Bravo\n\n5a. Delta [Action: Approve]"

    def test_looks_like_binary_body_branches(self) -> None:
        assert looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", _url("http://x/z.zip")) is True
        assert looks_like_binary_body(b"PK\x03\x04data", "text/html", _url("http://x/")) is False
        assert looks_like_binary_body(b"data", "application/json", _url("http://x/")) is False
        assert looks_like_binary_body(b"data", "application/octet-stream", _url("http://x/")) is True
        assert looks_like_binary_body(b"data", "application/pdf", _url("http://x/a.pdf")) is False

    def test_format_extracted_document_text_range_action(self) -> None:
        text = "1a - 1c Approve\n1a.\nBuild the thing\n1b.\nFix the bug\n1c.\nShip it"
        out = format_extracted_document_text(text, source_url="http://x/")
        assert "Approve" in out

    def test_should_join_document_paragraph_heading(self) -> None:
        # Kept as the negative half of the heading rule from the pure-parsing side.
        # The heading and the prose stay two records, so the heading did not absorb
        # what followed it.
        out = format_extracted_document_text("Section A\nsome prose", source_url="http://x/")
        assert out == "Section A\n\nsome prose"

    def test_extract_frames_skips_bad_and_dupes(self) -> None:
        html = (
            '<iframe src="http://same.example/f"></iframe>'
            '<iframe src="javascript:alert(1)"></iframe>'
            '<iframe src="http://same.example/f"></iframe>'
        )
        walkable = _walkable(
            html,
            _url("http://same.example/"),
            None,
        )
        assert walkable is not None
        frames = frames_from(walkable, _url("http://same.example/"))
        assert len(frames) == 1

    def test_extract_discovered_links_skips_bad_scheme(self) -> None:
        html = '<a href="mailto:a@b.com">x</a><a href="http://other.example/p">y</a>'
        links, count, truncated = extract_discovered_links(html, _url("http://example.com/"))
        assert len(links) == 1

    def test_frame_collector_ignores_frames_without_src(self) -> None:
        collector = _FrameCollector(_url("http://x/"))
        collector.feed('<frame></frame><iframe src="http://x/f"></iframe>')
        assert len(collector.frames) == 1


# The three discovery probes turned on together, as one named policy fragment.
# Every site that asks for full discovery used to re-spell the same three
# booleans, and the interesting half of those tests is the diagnostics, not
# which probes were enabled.
DISCOVERY_ALL_ON = {"robots": True, "sitemaps": True, "feeds": True}
# The page every discovery test below is fetched from, advertising one feed.
# The ``type`` is what makes the alternate link a FEED rather than a generic
# one, so a test that wants discovery to report a feed names that here once.
FEED_ADVERTISING_PAGE = (
    200,
    b'<html><body><link rel="alternate" type="application/rss+xml"'
    b' href="http://example.com/feed.xml"></body></html>',
    "text/html",
)

# The URL the default single-page drive fetches, and therefore the one a caller
# has to differ from for ``final_url`` to mean anything.
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

    Module-level rather than a method on the class that owns most of the callers,
    because the discovery-diagnostics tests below need the same drive and were
    reaching it by instantiating that test class -- a test borrowing another
    test's harness, which reads as a second owner of the same setup.

    ``final_url`` is honoured: ``scripted_fetcher`` cannot answer with a final URL
    other than the requested one, and a caller that asks for a different one (the
    ``.zip`` final URL the binary-body test is named for) gets a prebuilt
    ``HttpFetchResult``, which ``routed_fetcher`` returns verbatim. It used to be
    accepted and dropped, so that test asserted over a redirected page it thought
    it was serving and nobody could tell from reading it.
    """
    if fetcher is None:
        fetcher = (
            scripted_fetcher(status=status, ctype=ctype, body=body)
            if final_url == _PAGE_URL
            else routed_fetcher(
                default=make_fetch(status=status, body=body, ctype=ctype, final_url=final_url),
            )
        )

    # A policy partial merges as a raw dict and is read once, the way the wire
    # merges it. Mutating the model in place is not an option now that
    # ``make_base_policy`` returns a validated CrawlPolicy: there is no
    # re-validation on a mutated field, so an override could leave the policy
    # holding a value its own bounds would have rejected.
    base = make_base_policy()
    pol = read_policy({**base.model_dump(), **policy}) if policy else base
    return await http_strategy_result(
        request=strategy_request("http://example.com/", pol, strategy),
        http_fetcher=fetcher,
    )


async def _diagnostics_for_failed_probes(fetcher, **policy):
    """Full discovery whose probes fail: the ``(result, diagnostics)`` pair.

    Discovery probes fail; the page fetch does not. The point of both tests that
    call this is that a failed probe is recorded as diagnostics and does not fail
    the crawl, so the crawl config and those two assertions are written once here
    and each test says only WHICH diagnostic it is about. A copy that drops the
    limits silently runs the default ones, and a copy that drops
    ``DISCOVERY_ALL_ON`` passes a test that discovered nothing.
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
        # A raw ConnectionError classifies specifically as connection_error, not
        # the generic network_error tail: the old broad-except sink hardcoded
        # network_error and masked the real code; the shared primitive now
        # surfaces it.
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
        # The feed the page advertises is still discovered: a failed robots or
        # sitemap probe costs a diagnostic, not the discovery pass.
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
        # A strategy outside CrawlStrategyName is rejected at construction of the
        # attempt: the strategy axis is a Literal, so an invalid value cannot
        # reach a result and callers see a ValueError naming the field.
        with pytest.raises(ValueError, match=r"\.strategy="):
            await run_chain(
                fetcher=ok_fetcher,
                policy=make_base_policy().model_copy(update={"strategies": ["NOT_A_STRATEGY"]}),
            )


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

    def test_checked_payload_urls_is_the_shared_batch_gate(self) -> None:
        """One chokepoint returns in-range urls and rejects oversized batches."""
        from app.crawl.payload_helpers import checked_payload_urls

        within = {"urls": ["http://example.com/a", "http://example.com/a", "http://example.com/b"]}
        assert checked_payload_urls(
            within,
            10,
            host_policy=HostPolicy.unrestricted(),
        ) == ["http://example.com/a", "http://example.com/b"]
        oversized = {"urls": [f"http://example.com/{i}" for i in range(11)]}
        with pytest.raises(ValueError, match=r"^batch_size_exceeded$"):
            checked_payload_urls(
                oversized,
                10,
                host_policy=HostPolicy.unrestricted(),
            )

    def test_checked_payload_urls_accepts_a_forwarded_batch_without_rederiving(self) -> None:
        """Tier-2 the endpoint gate's result may be handed to the engine so
        the batch is parsed once per request. A forwarded batch is returned as
        the job set without re-deriving from the payload..."""
        from app.crawl.payload_helpers import checked_payload_urls

        forwarded = ["http://example.com/a", "http://example.com/b"]
        oversized_payload = {"urls": [f"http://example.com/{i}" for i in range(11)]}
        assert (
            checked_payload_urls(
                oversized_payload,
                10,
                urls=forwarded,
                host_policy=HostPolicy.unrestricted(),
            )
            == forwarded
        )

    def test_forwarded_batch_still_faces_the_cap(self) -> None:
        """...but the cap still applies to a forwarded batch (invariant: the
        engine-internal ceiling cannot be bypassed by supplying urls)."""
        from app.crawl.payload_helpers import checked_payload_urls

        forwarded = [f"http://example.com/{i}" for i in range(11)]
        with pytest.raises(ValueError, match=r"^batch_size_exceeded$"):
            checked_payload_urls(
                {"url": "http://example.com/x"},
                10,
                urls=forwarded,
                host_policy=HostPolicy.unrestricted(),
            )

    async def test_crawl_request_consumes_the_forwarded_batch(self) -> None:
        """Engine handoff: with urls supplied, those URLs become the job set --
        the engine does not re-run _payload_urls over the payload."""
        fetched: list[str] = []

        spy_fetcher = routed_fetcher({}, calls=fetched, default={})

        base = make_base_policy()
        policy = read_policy(
            {**base.model_dump(), "limits": {**base.limits.model_dump(), "respect_robots": False}}
        )
        result = await crawl_request(
            {"urls": ["http://example.com/a", "http://example.com/a/?utm_source=tw"], "policy": policy},
            urls=["http://example.com/a"],
            http_fetcher=spy_fetcher,
            cache=HttpCache(),
        )
        assert fetched == ["http://example.com/a"]
        assert result["submitted_count"] == 1
        assert result["results"][0].status == "success"

    async def test_invalid_url_raises(self) -> None:
        payload = {"url": "not a url", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}}
        with pytest.raises(ValueError, match=r"^invalid_url"):
            await crawl_request(payload, http_fetcher=ok_fetcher, browser_worker=None)

    async def test_success_with_injected_fetcher(self) -> None:
        payload = {"url": "http://example.com/", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}}
        result = await crawl_request(payload, http_fetcher=ok_fetcher, browser_worker=None)
        assert result["submitted_count"] == 1
        assert result["results"][0].status == "success"


class TestFetchHelperUtilities:
    def test_form_collector_select_textarea_button(self) -> None:
        walkable = _walkable(
            "<form action='/submit' method='post'>"
            "<input type='text' name='q' value='a'>"
            "<select name='s'><option value='1'>x</option></select>"
            "<textarea name='t'>hi</textarea>"
            "<button name='b' value='go'>Go</button>"
            "</form>",
            _url("http://x/"),
            None,
        )
        assert walkable is not None
        forms = forms_from(walkable)
        assert len(forms) == 1
        tags = {c["tag"] for c in forms[0]["controls"]}
        assert {"input", "select", "textarea", "button"} <= tags

    def test_extract_discovered_links_skips_ftp_scheme(self) -> None:
        links, count, truncated = extract_discovered_links(
            "<a href='ftp://host/data'>x</a><a href='https://ok/x'>ok</a>", _url("http://x/")
        )
        assert all("ftp://" not in getattr(link, "url", str(link)) for link in links)
        assert any("https://ok/x" in getattr(link, "url", str(link)) for link in links)

    def test_public_participation_breaks_item_title_collection(self) -> None:
        # A boilerplate section marker ends the title run rather than joining it, so
        # the marker is dropped and the text after it is not mistaken for more title.
        out = format_extracted_document_text(
            "1a.\nBuild\nPublic Participation\nNext item", source_url="http://x/"
        )
        assert out == "1a. Build\n\nNext item"

    def test_looks_like_binary_body_magic_without_zip_extension(self) -> None:
        # PK magic with a non-.zip URL still classifies as binary.
        assert looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", _url("http://x/file")) is True

    async def test_discovery_robots_success_and_malformed_crawl_delay(self) -> None:
        fetcher = routed_fetcher(
            {
                "/robots.txt": (200, b"User-agent: *\nCrawl-delay: notanumber\n", "text/plain"),
                "sitemap": ConnectionError("no sitemap"),
            },
            default=FEED_ADVERTISING_PAGE,
        )

        result, diagnostics = await _diagnostics_for_failed_probes(fetcher)
        assert diagnostics["robots"].get("crawl_delay_seconds") is None
        assert diagnostics["discovery"]["feeds"]


class TestCallWorkerContract:
    async def test_awaits_coroutine_worker_directly(self) -> None:
        await assert_coroutine_worker_is_awaited_directly()

    async def test_runs_sync_worker_in_thread(self) -> None:
        await assert_sync_worker_runs_once_in_a_thread()


class TestFingerprintInjectionRotation:
    def test_user_agent_pool_rotates_per_domain_and_is_stable(self):
        profile = {"user_agents": ["UA-A", "UA-B", "UA-C"]}
        # Use more hosts than pool size so rotation is clearly visible.
        hosts = [f"{c}.com" for c in "abcdef"]
        seen = {d: _select_fingerprint(profile, _url(f"http://{d}/")).get("user_agent") for d in hosts}
        # Rotation: different hosts get a variety of UAs (not all identical)
        assert len(set(seen.values())) > 1
        # Stability: same host -> same UA across calls (keeps content-cache key stable)
        assert _select_fingerprint(profile, _url("http://a.com/"))["user_agent"] == seen["a.com"]
        assert _select_fingerprint(profile, _url("http://a.com/"))["user_agent"] == seen["a.com"]

    def test_fingerprint_rotation_changes_accept_language_per_domain(self):
        base = {"request_profile": {"fingerprint_rotation": True}, "headers": {}}
        langs = {
            d: headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(base), _url(f"http://{d}/"))[
                "Accept-Language"
            ]
            for d in ("a.com", "b.com", "c.com", "d.com", "e.com")
        }
        # Across 5 distinct hosts the built-in pool (size 5) yields variety
        assert len(set(langs.values())) > 1
        # Same host is stable
        again = headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(base), _url("http://a.com/"))[
            "Accept-Language"
        ]
        assert again == langs["a.com"]

    def test_default_request_has_no_fingerprint_rotation(self):
        headers = headers_for_strategy(
            "HTTP_DECLARED_CRAWLER",
            read_policy({"request_profile": {}, "headers": {}}),
            _url("http://x/"),
        )
        assert headers["Accept-Language"] == "en-US,en;q=0.9"
        assert "Mozilla" not in headers["User-Agent"]

    def test_fingerprint_injected_user_agent_lands_in_headers(self):
        profile = {"request_profile": {"user_agents": ["Rotated-UA-1", "Rotated-UA-2"]}, "headers": {}}
        h1 = headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(profile), _url("http://a.com/"))
        h2 = headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(profile), _url("http://b.com/"))
        assert h1["User-Agent"] == "Rotated-UA-1"
        assert h2["User-Agent"] == "Rotated-UA-2"

    def test_explicit_headers_still_override_fingerprint(self):
        profile = {"request_profile": {"user_agents": ["Rotated-UA-1"]}, "headers": {"User-Agent": "forced"}}
        h = headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(profile), _url("http://a.com/"))
        assert h["User-Agent"] == "forced"


class TestQualityScore:
    def test_http_quality_score_failed(self):
        from app.crawl.strategies.body_decode import quality_score

        score = quality_score("failed", "network_error", None, None, None)
        assert score == 0.0

    def test_http_quality_score_empty_text(self):
        from app.crawl.strategies.body_decode import quality_score

        score = quality_score("success", "rendered", "", None, 200)
        assert score > 0.0


class TestFetchHelpersEdges:
    def test_cookie_header_per_host(self):
        assert (
            _cookie_header_for_url(
                {"cookie_jar": "per_host", "cookies": {"e.com": {"a": "1"}}},
                _url("http://e.com/"),
            )
            == "a=1"
        )

    def test_headers_cffi_impersonate(self):
        headers = headers_for_strategy(
            HTTP_CFFI_IMPERSONATE,
            read_policy({"request_profile": {}, "headers": {}}),
            _url("http://e.com/"),
        )
        assert "User-Agent" in headers
        # Firefox identity (matching the firefox135 impersonation) sends no
        # Sec-CH-UA client hints -- those are Chromium-only.
        assert "sec-ch-ua" not in {k.lower() for k in headers}
        assert "Firefox" in headers["User-Agent"]

    def test_headers_cookie_added(self):
        headers = headers_for_strategy(
            HTTP_DECLARED_CRAWLER,
            read_policy(
                {
                    "request_profile": {"cookie_jar": "per_host", "cookies": {"e.com": {"a": "1"}}},
                    "headers": {},
                }
            ),
            _url("http://e.com/"),
        )
        assert headers.get("Cookie") == "a=1"

    def test_status_for_not_modified(self):
        assert status_for_classification("not_modified") == "success"

    def test_quality_frameset_low_text(self):
        score = quality_score("success", "rendered", "tiny", "<frameset></frameset>", 200)
        assert score <= 0.4

    @pytest.mark.parametrize(
        "text,html", [(None, None), ("", None), ("hi", "<p>x</p>"), ("a" * 30, "<html><body>x</body></html>")]
    )
    def test_quality_score_reuses_precomputed_metrics(self, text, html):
        """Dup8: threading the diagnostics metrics in must not change the score."""
        from app.crawl.strategies.body_decode import content_metrics

        metrics = content_metrics(text, html)
        direct = quality_score("success", "rendered", text, html, 200)
        threaded = quality_score("success", "rendered", text, html, 200, content_metrics=metrics)
        assert threaded == direct

    def test_quality_score_ignores_metrics_when_not_rendered(self):
        """The early return must not read the metrics dict (None is never indexed)."""
        assert quality_score("failed", "network_error", None, None, None, content_metrics=None) == 0.0

    def test_decode_body_gzip_content_encoding(self):
        # Decompression happens at the capped chokepoint, then decode.
        body = gzip.compress(b"hello world")
        out = decode_body(decompress_body(body, "gzip", max_output_bytes=1_000_000), "text/html")
        assert out == "hello world"


class TestAcceptEncodingAdvertisesOnlyWhatWeCanDecode:
    """Asking for an encoding we cannot decode is a self-inflicted failure.

    ``Accept-Encoding`` must not name a coding outside
    ``app.crawl.decompression._DECODABLE_ENCODINGS``: a server told to send a body
    this process will then refuse or pass through whole fails the crawl on a
    request we chose to make that way.

    Every decoder in the advertised list is a declared dependency, so the list is
    unconditional. It was once gated on a brotli availability probe, and the two
    tests below are the residue of that: one skipped itself when the probe said
    no, and one monkeypatched the probe to ``False`` to assert a deployment
    requirements.txt does not describe. Both are gone; what is asserted instead is
    that the unconditional list is unconditionally decodable.
    """

    def test_br_is_advertised(self):
        headers = headers_for_strategy(HTTP_DECLARED_CRAWLER, read_policy({"headers": {}}), _url("http://e.com/"))
        assert "br" in headers["Accept-Encoding"]

    def test_every_advertised_encoding_is_always_decodable(self):
        # A real round-trip through the real decompressor for every token the
        # header names. This is the invariant the old two tests gestured at
        # without checking: the claim on the wire and the decoder must agree.
        import gzip
        import zlib

        from app.crawl.decompression import decompress_body

        payload = b"<html>hello</html>"
        for encoding, compressed in (
            ("gzip", gzip.compress(payload)),
            ("deflate", zlib.compress(payload)),
            ("br", brotli.compress(payload)),
        ):
            assert decompress_body(compressed, encoding, max_output_bytes=1024) == payload

    def test_the_header_advertises_only_decodable_encodings(self):
        # The negative direction: the list is not merely "includes br", it names
        # nothing outside the decoder set, so a future token cannot slip in.
        from app.crawl.decompression import _DECODABLE_ENCODINGS

        headers = headers_for_strategy(HTTP_DECLARED_CRAWLER, read_policy({"headers": {}}), _url("http://e.com/"))
        advertised = {token.strip() for token in headers["Accept-Encoding"].split(",")}
        assert advertised <= _DECODABLE_ENCODINGS
