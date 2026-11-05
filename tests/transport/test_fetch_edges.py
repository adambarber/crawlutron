"""Crawl requests, fingerprints and encodings advertise honestly."""

import gzip

import brotli
import pytest

from crawlutron.admission.payload_helpers import checked_payload_urls
from crawlutron.admission.policy import read_policy
from crawlutron.config.settings import CrawlerSettings
from crawlutron.extraction.content import forms_from
from crawlutron.extraction.document_text import format_extracted_document_text
from crawlutron.extraction.trees import walkable
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.json_value import JsonValue
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.request_profile_record import RequestProfileRecord
from crawlutron.orchestration.engine import crawl_request
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.fetch_policy import looks_like_binary_body
from crawlutron.transport.request_profile import (
    cookie_header_for_url,
    headers_for_strategy,
    select_fingerprint,
)
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from crawlutron.vocabulary.taxonomy import status_for_classification
from tests._extraction_helpers import extract_discovered_links
from tests._fetch_doubles import FetcherDouble, make_fetch
from tests._http_fetch_doubles import FEED_ADVERTISING_PAGE
from tests._http_fetch_doubles import diagnostics_for_failed_probes as _diagnostics_for_failed_probes
from tests._http_test_helpers import (
    HTTP_CFFI_IMPERSONATE,
    decode_body,
    decompress_body,
    quality_score,
)
from tests._policy_factories import canonical, make_base_policy
from tests._worker_doubles import (
    assert_coroutine_worker_is_awaited_directly,
    assert_sync_worker_runs_once_in_a_thread,
)


def _ok_fetcher(url, *_):
    return make_fetch(url=url)


class TestCrawlRequest:
    async def test_batch_size_exceeded(self) -> None:
        settings = CrawlerSettings.from_mapping()
        payload = {
            # Distinct URLs: the cap counts unique URLs (exact duplicates collapse in _payload_urls before
            # this check).
            "urls": [f"http://example.com/{i}" for i in range(settings.max_batch_size + 1)],
            "policy": {"strategies": [HTTP_DECLARED_CRAWLER]},
        }
        with pytest.raises(ValueError, match=r"^batch_size_exceeded$"):
            await crawl_request(payload, settings=settings, http_fetcher=_ok_fetcher, browser_worker=None)

    def test_checked_payload_urls_is_the_shared_batch_gate(self) -> None:
        """One chokepoint returns in-range urls and rejects oversized batches."""
        within = CrawlRequest(urls=["http://example.com/a", "http://example.com/a", "http://example.com/b"])
        pairs = checked_payload_urls(within, 10, host_policy=HostPolicy.unrestricted())
        assert [text for text, _ in pairs] == ["http://example.com/a", "http://example.com/b"]
        oversized = CrawlRequest(urls=[f"http://example.com/{i}" for i in range(11)])
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
        policy = HostPolicy.unrestricted()
        forwarded = [
            ("http://example.com/a", NormalizedUrl("http://example.com/a", policy=policy)),
            ("http://example.com/b", NormalizedUrl("http://example.com/b", policy=policy)),
        ]
        oversized_payload = CrawlRequest(urls=[f"http://example.com/{i}" for i in range(11)])
        outcome = checked_payload_urls(
            oversized_payload,
            10,
            url_pairs=forwarded,
            host_policy=policy,
        )
        assert outcome == forwarded

    def test_forwarded_batch_still_faces_the_cap(self) -> None:
        """...but the cap still applies to a forwarded batch (invariant: the
        engine-internal ceiling cannot be bypassed by supplying url_pairs)."""
        policy = HostPolicy.unrestricted()
        forwarded = [
            (f"http://example.com/{i}", NormalizedUrl(f"http://example.com/{i}", policy=policy))
            for i in range(11)
        ]
        with pytest.raises(ValueError, match=r"^batch_size_exceeded$"):
            checked_payload_urls(
                CrawlRequest(url="http://example.com/x"),
                10,
                url_pairs=forwarded,
                host_policy=policy,
            )

    async def test_crawl_request_consumes_the_forwarded_batch(self) -> None:
        """Engine handoff: with url_pairs supplied, those URLs become the job set --
        the engine does not re-run _payload_urls over the payload."""
        fetched: list[str] = []

        spy_fetcher = FetcherDouble(lambda url, *_: {}, fetched)

        base = make_base_policy()
        policy = read_policy(
            {**base.model_dump(), "limits": {**base.limits.model_dump(), "respect_robots": False}}
        )
        forwarded_pair = (
            "http://example.com/a",
            NormalizedUrl("http://example.com/a", policy=HostPolicy.unrestricted()),
        )
        result = await crawl_request(
            {"urls": ["http://example.com/a", "http://example.com/a/?utm_source=tw"], "policy": policy},
            url_pairs=[forwarded_pair],
            http_fetcher=spy_fetcher,
            cache=HttpCache(),
        )
        assert fetched == ["http://example.com/a"]
        assert result["submitted_count"] == 1
        assert result["results"][0].status == "success"

    async def test_invalid_url_raises(self) -> None:
        payload = {"url": "not a url", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}}
        with pytest.raises(ValueError, match=r"^invalid_url"):
            await crawl_request(payload, http_fetcher=_ok_fetcher, browser_worker=None)

    async def test_success_with_injected_fetcher(self) -> None:
        payload = {"url": "http://example.com/", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}}
        result = await crawl_request(payload, http_fetcher=_ok_fetcher, browser_worker=None)
        assert result["submitted_count"] == 1
        assert result["results"][0].status == "success"


class TestFetchHelperUtilities:
    def test_form_collector_select_textarea_button(self) -> None:
        walked = walkable(
            "<form action='/submit' method='post'>"
            "<input type='text' name='q' value='a'>"
            "<select name='s'><option value='1'>x</option></select>"
            "<textarea name='t'>hi</textarea>"
            "<button name='b' value='go'>Go</button>"
            "</form>",
            canonical("http://x/"),
            None,
        )
        assert walked is not None
        forms = forms_from(walked)
        assert len(forms) == 1
        tags = {c["tag"] for c in forms[0]["controls"]}
        assert {"input", "select", "textarea", "button"} <= tags

    def test_extract_discovered_links_skips_ftp_scheme(self) -> None:
        links, count, truncated = extract_discovered_links(
            "<a href='ftp://host/data'>x</a><a href='https://ok/x'>ok</a>", canonical("http://x/")
        )
        assert all("ftp://" not in getattr(link, "url", str(link)) for link in links)
        assert any("https://ok/x" in getattr(link, "url", str(link)) for link in links)

    def test_public_participation_breaks_item_title_collection(self) -> None:
        # A boilerplate section marker ends the title run rather than joining it, so the marker is dropped and
        # the text after it is not mistaken for more title.
        out = format_extracted_document_text(
            "1a.\nBuild\nPublic Participation\nNext item", source_url="http://x/"
        )
        assert out == "1a. Build\n\nNext item"

    def test_looks_like_binary_body_magic_without_zip_extension(self) -> None:
        # PK magic with a non-.zip URL still classifies as binary.
        assert (
            looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", canonical("http://x/file"))
            is True
        )

    async def test_discovery_robots_success_and_malformed_crawl_delay(self) -> None:
        _robots_ok = (200, b"User-agent: *\nCrawl-delay: notanumber\n", "text/plain")
        fetcher = FetcherDouble(
            lambda url, _sent_headers, *_: (
                _robots_ok
                if "/robots.txt" in url.raw
                else ConnectionError("no sitemap")
                if "sitemap" in url.raw
                else FEED_ADVERTISING_PAGE
            )
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
        profile: RequestProfileRecord = {"user_agents": ["UA-A", "UA-B", "UA-C"]}
        # Use more hosts than pool size so rotation is clearly visible.
        hosts = [f"{c}.com" for c in "abcdef"]
        seen = {d: select_fingerprint(profile, canonical(f"http://{d}/")).get("user_agent") for d in hosts}
        # Rotation: different hosts get a variety of UAs (not all identical)
        assert len(set(seen.values())) > 1
        # Stability: same host -> same UA across calls (keeps content-cache key stable)
        assert select_fingerprint(profile, canonical("http://a.com/"))["user_agent"] == seen["a.com"]
        assert select_fingerprint(profile, canonical("http://a.com/"))["user_agent"] == seen["a.com"]

    def test_fingerprint_rotation_changes_accept_language_per_domain(self):
        base: dict[str, JsonValue] = {"request_profile": {"fingerprint_rotation": True}, "headers": {}}
        langs = {
            d: headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(base), canonical(f"http://{d}/"))[
                "Accept-Language"
            ]
            for d in ("a.com", "b.com", "c.com", "d.com", "e.com")
        }
        # Across 5 distinct hosts the built-in pool (size 5) yields variety
        assert len(set(langs.values())) > 1
        # Same host is stable
        again = headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(base), canonical("http://a.com/"))[
            "Accept-Language"
        ]
        assert again == langs["a.com"]

    def test_default_request_has_no_fingerprint_rotation(self):
        headers = headers_for_strategy(
            "HTTP_DECLARED_CRAWLER",
            read_policy({"request_profile": {}, "headers": {}}),
            canonical("http://x/"),
        )
        assert headers["Accept-Language"] == "en-US,en;q=0.9"
        assert "Mozilla" not in headers["User-Agent"]

    def test_fingerprint_injected_user_agent_lands_in_headers(self):
        profile: dict[str, JsonValue] = {
            "request_profile": {"user_agents": ["Rotated-UA-1", "Rotated-UA-2"]},
            "headers": {},
        }
        h1 = headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(profile), canonical("http://a.com/"))
        h2 = headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(profile), canonical("http://b.com/"))
        assert h1["User-Agent"] == "Rotated-UA-1"
        assert h2["User-Agent"] == "Rotated-UA-2"

    def test_explicit_headers_still_override_fingerprint(self):
        profile: dict[str, JsonValue] = {
            "request_profile": {"user_agents": ["Rotated-UA-1"]},
            "headers": {"User-Agent": "forced"},
        }
        h = headers_for_strategy("HTTP_DECLARED_CRAWLER", read_policy(profile), canonical("http://a.com/"))
        assert h["User-Agent"] == "forced"


class TestQualityScore:
    def test_http_quality_score_failed(self):
        from crawlutron.transport.body_decode import quality_score

        score = quality_score("failed", "network_error", None, None, None)
        assert score == 0.0

    def test_http_quality_score_empty_text(self):
        from crawlutron.transport.body_decode import quality_score

        score = quality_score("success", "rendered", "", None, 200)
        assert score > 0.0


class TestFetchHelpersEdges:
    def test_cookie_header_per_host(self):
        assert (
            cookie_header_for_url(
                {"cookie_jar": "per_host", "cookies": {"e.com": {"a": "1"}}},
                canonical("http://e.com/"),
            )
            == "a=1"
        )

    def test_headers_cffi_impersonate(self):
        headers = headers_for_strategy(
            HTTP_CFFI_IMPERSONATE,
            read_policy({"request_profile": {}, "headers": {}}),
            canonical("http://e.com/"),
        )
        assert "User-Agent" in headers
        # Firefox identity (matching the firefox135 impersonation) sends no Sec-CH-UA client hints -- those
        # are Chromium-only.
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
            canonical("http://e.com/"),
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
        from crawlutron.transport.body_decode import content_metrics

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

    ``Accept-Encoding`` must name only a decodable coding; the advertised list is
    unconditional (a decoder is a declared dependency), not probe-gated.
    """

    def test_br_is_advertised(self):
        headers = headers_for_strategy(
            HTTP_DECLARED_CRAWLER, read_policy({"headers": {}}), canonical("http://e.com/")
        )
        assert "br" in headers["Accept-Encoding"]

    def test_every_advertised_encoding_is_always_decodable(self):
        # A real round-trip through the real decompressor for every token the header names
        import gzip
        import zlib

        from crawlutron.transport.decompression import decompress_body

        payload = b"<html>hello</html>"
        for encoding, compressed in (
            ("gzip", gzip.compress(payload)),
            ("deflate", zlib.compress(payload)),
            ("br", brotli.compress(payload)),
        ):
            assert decompress_body(compressed, encoding, max_output_bytes=1024) == payload

    def test_the_header_advertises_only_decodable_encodings(self):
        # The negative direction: the list is not merely "includes br", it names nothing outside the decoder
        # set, so a future token cannot slip in.
        from crawlutron.transport.decompression import DECODABLE_ENCODINGS

        headers = headers_for_strategy(
            HTTP_DECLARED_CRAWLER, read_policy({"headers": {}}), canonical("http://e.com/")
        )
        advertised = {token.strip() for token in headers["Accept-Encoding"].split(",")}
        assert advertised <= DECODABLE_ENCODINGS
