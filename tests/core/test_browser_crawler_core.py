import asyncio
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

CRAWLER_APP = Path(__file__).resolve().parents[2]
SHARED_PYTHON = Path(__file__).resolve().parents[4] / "docker" / "shared" / "python"

from app.browser import BrowserRenderer
from app.classify import classify_rendered_page, classify_rendered_page_with_evidence
from app.models import CrawlJob, CrawlResult, HttpFetchResult, RenderOptions
from app.scheduler import DomainScheduler
from app.service import _extract_discovered_links, _format_extracted_document_text, crawl_request
from app.settings import CrawlerSettings
from app.validation import normalize_public_http_url, registrable_domain
from app.constants import (
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
    EXTRACTION_DOCUMENT,
    EXTRACTION_FULL,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_DOM_TEXT,
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)


def make_fetcher(status_code: int = 200, content_type: str = "text/html", body: bytes = b"",
                 elapsed_ms: int = 1, final_url: str | None = None, delay: float = 0.0):
    def _fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
        if delay:
            time.sleep(delay)
        return HttpFetchResult(
            url=url,
            final_url=final_url if final_url is not None else url,
            status_code=status_code,
            headers={"content-type": content_type},
            body=body,
            elapsed_ms=elapsed_ms,
        )
    return _fetcher


def make_renderer(page, **kwargs):
    mock_browser = AsyncMock()
    mock_browser.new_page = AsyncMock(return_value=page)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_browser)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return BrowserRenderer(browser_factory=lambda **kw: ctx, sleep=lambda _: None, **kwargs)


def run_crawl(url: str, policy: dict, http_fetcher=None, browser_worker=None, cffi_fetcher=None):
    return asyncio.run(
        crawl_request(
            {"url": url, "policy": policy},
            settings=CrawlerSettings(per_domain_delay_ms=0),
            http_fetcher=http_fetcher,
            cffi_fetcher=cffi_fetcher,
            browser_worker=browser_worker,
        )
    )


def ok_worker():
    async def worker(job):
        return CrawlResult(
            url=job.url,
            status="success",
            classification="rendered",
            final_url=job.url,
            html="<html>ok</html>",
            text="ok",
            attempts=1,
            elapsed_ms=1,
            domain=job.domain,
        )
    return worker


def make_browser_worker(html: str | None = "", text: str | None = "", final_url=None,
                        elapsed_ms: int = 5, metadata=None, status: str = "success",
                        classification: str = "rendered"):
    def _worker(job):
        kwargs = dict(
            url=job.url,
            status=status,
            classification=classification,
            final_url=final_url if final_url is not None else job.url,
            html=html,
            text=text,
            attempts=1,
            elapsed_ms=elapsed_ms,
            domain=job.domain,
        )
        if metadata is not None:
            kwargs["metadata"] = metadata
        return CrawlResult(**kwargs)
    return _worker


def run_cooldown_case(test, cooldown, expect_wait, past=False, delay_seconds=0.0):
    scheduler = DomainScheduler(global_limit=2, per_domain_limit=1, delay_seconds=delay_seconds)
    if past:
        scheduler._domain_cooldowns["example.com"] = time.time() - 1.0
    else:
        scheduler.set_domain_cooldown("example.com", cooldown)
    start = time.time()
    worker = ok_worker()
    jobs = [CrawlJob(index=0, url="https://example.com/page1", domain="example.com", options=RenderOptions())]
    results = asyncio.run(scheduler.run(jobs, worker))
    elapsed = time.time() - start
    if expect_wait:
        test.assertGreaterEqual(elapsed, 0.05)
    else:
        test.assertLess(elapsed, 0.05)
    test.assertEqual(len(results), 1)
    test.assertEqual(results[0].status, "success")


def assert_browser_fallback(test, browser_worker, expected_text):
    fetcher = make_fetcher(status_code=403, content_type="text/html", body=b"Access Denied", elapsed_ms=2)
    response = run_crawl(
        "https://records.example.gov/search",
        {"strategies": [HTTP_CFFI_IMPERSONATE, BROWSER_HEADLESS]},
        cffi_fetcher=fetcher,
        browser_worker=browser_worker,
    )
    result = response["results"][0]
    test.assertEqual(result["status"], "success")
    test.assertEqual(result["selected_strategy"], BROWSER_HEADLESS)
    test.assertEqual(
        [attempt["strategy"] for attempt in result["attempted_strategies"]],
        [HTTP_CFFI_IMPERSONATE, BROWSER_HEADLESS],
    )
    test.assertEqual(result["attempted_strategies"][0]["status"], "blocked")
    test.assertEqual(result["text"], expected_text)
    return result


class BrowserCrawlerCoreTest(unittest.TestCase):
    def test_direct_http_crawl_work_does_not_block_event_loop(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="text/html",
            body=b"<html><body>Responsive direct HTTP</body></html>",
            elapsed_ms=100,
            delay=0.1,
        )

        async def run_probe() -> int:
            crawl_task = asyncio.create_task(
                crawl_request(
                    {"url": "https://records.example.gov/search", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}},
                    settings=CrawlerSettings(per_domain_delay_ms=0),
                    http_fetcher=fetcher,
                )
            )
            ticks = 0
            while not crawl_task.done():
                ticks += 1
                await asyncio.sleep(0.01)
            await crawl_task
            return ticks

        self.assertGreaterEqual(asyncio.run(run_probe()), 3)

    def test_sync_browser_crawl_work_does_not_block_event_loop(self) -> None:
        def browser_worker(job: CrawlJob) -> CrawlResult:
            time.sleep(0.1)
            return CrawlResult(
                url=job.url,
                status="success",
                classification="rendered",
                final_url=job.url,
                html="<html><body>Responsive browser</body></html>",
                text="Responsive browser",
                attempts=1,
                elapsed_ms=100,
                domain=job.domain,
            )

        async def run_probe() -> int:
            crawl_task = asyncio.create_task(
                crawl_request(
                    {"url": "https://records.example.gov/search", "policy": {"strategies": [BROWSER_HEADLESS]}},
                    settings=CrawlerSettings(per_domain_delay_ms=0),
                    browser_worker=browser_worker,
                )
            )
            ticks = 0
            while not crawl_task.done():
                ticks += 1
                await asyncio.sleep(0.01)
            await crawl_task
            return ticks

        self.assertGreaterEqual(asyncio.run(run_probe()), 3)

    def test_url_validation_accepts_public_http_urls_and_extracts_registrable_domain(self) -> None:
        normalized = normalize_public_http_url("https://records.city.example.gov/search?q=permit")

        self.assertEqual(normalized, "https://records.city.example.gov/search?q=permit")
        self.assertEqual(registrable_domain(normalized), "example.gov")

    def test_url_validation_rejects_private_network_targets(self) -> None:
        for url in [
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://10.0.0.5/secret",
            "file:///etc/passwd",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    normalize_public_http_url(url)

    def test_url_validation_strips_source_trailing_colon_artifacts(self) -> None:
        self.assertEqual(
            normalize_public_http_url("https://www.example.gov/path/document.pdf:"),
            "https://www.example.gov/path/document.pdf",
        )
        self.assertEqual(
            normalize_public_http_url("https://www.example.gov/document.pdf?t=202606081542580:"),
            "https://www.example.gov/document.pdf?t=202606081542580",
        )

    def test_url_validation_rejects_missing_hostname(self) -> None:
        for url in [
            "http:///path",
            "https://",
            "http://?query=1",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as cm:
                    normalize_public_http_url(url)
                self.assertEqual(str(cm.exception), "invalid_url")

    def test_url_validation_normalizes_port_in_netloc(self) -> None:
        normalized = normalize_public_http_url("https://example.gov:8443/path")
        self.assertEqual(normalized, "https://example.gov:8443/path")

        # Non-standard port should be preserved
        normalized = normalize_public_http_url("http://example.com:8080/search")
        self.assertEqual(normalized, "http://example.com:8080/search")

    def test_url_normalization_is_single_source_of_truth_for_trailing_slash_and_default_ports(self) -> None:
        # boston.gov/ and boston.gov must collapse to the same normalized key.
        self.assertEqual(normalize_public_http_url("https://boston.gov/"), "https://boston.gov")
        self.assertEqual(normalize_public_http_url("https://boston.gov"), "https://boston.gov")
        # default ports are stripped (port 443 / 80)
        self.assertEqual(normalize_public_http_url("https://boston.gov:443/"), "https://boston.gov")
        self.assertEqual(normalize_public_http_url("http://boston.gov:80/"), "http://boston.gov")

    def test_registrable_domain_handles_two_part_host(self) -> None:
        self.assertEqual(registrable_domain("https://example.com"), "example.com")
        self.assertEqual(registrable_domain("https://sub.example.com"), "example.com")
        self.assertEqual(registrable_domain("https://a.b.c.example.com"), "example.com")

    def test_registrable_domain_handles_two_part_public_suffixes(self) -> None:
        self.assertEqual(registrable_domain("https://example.co.uk"), "example.co.uk")
        self.assertEqual(registrable_domain("https://sub.example.co.uk"), "example.co.uk")
        self.assertEqual(registrable_domain("https://a.b.example.org.uk"), "example.org.uk")
        self.assertEqual(registrable_domain("https://example.com.au"), "example.com.au")

    def test_classifier_does_not_treat_youtube_sign_in_copy_as_captcha(self) -> None:
        text = "Sign in to confirm you're not a bot. This helps protect our community. Learn more."

        self.assertEqual(classify_rendered_page("<html></html>", text, title="YouTube"), "login_required")

    def test_classifier_ignores_rate_limit_signatures_inside_scripts_when_visible_text_is_good(self) -> None:
        html = """
        <html><body><main>Permit program content</main>
        <script>var errors = { too_many: { message: "429: Too Many Requests" } };</script>
        </body></html>
        """

        self.assertEqual(classify_rendered_page(html, "Permit program content", title="Permits"), "rendered")

    def test_classifier_marks_visible_rate_limit_text(self) -> None:
        self.assertEqual(classify_rendered_page("<html></html>", "HTTP 429 Too Many Requests", title=""), "rate_limited")

    def test_real_fixture_corpus_manifest_documents_expected_behaviors(self) -> None:
        import json

        manifest = json.loads((CRAWLER_APP / "tests" / "fixtures" / "browser_crawler" / "manifest.json").read_text(encoding="utf-8"))

        fixture_names = {fixture["file"] for fixture in manifest["fixtures"]}
        self.assertIn("massgov_city_town_websites.browser.html", fixture_names)
        self.assertIn("massgov_municipal_databank.browser.html", fixture_names)
        self.assertIn("massgov_403.http.html", fixture_names)
        self.assertIn("worcester_primegov_city_council_agenda_74977.pdf", fixture_names)
        self.assertIn("worcester_primegov_city_council_agenda_74977.txt", fixture_names)
        databank = next(fixture for fixture in manifest["fixtures"] if fixture["file"] == "massgov_municipal_databank.browser.html")
        self.assertEqual(databank["expected_classification"], "rendered")
        self.assertEqual(databank["source"], "real_browser_crawl")
        self.assertIn("direct_http_blocked_browser_success", databank["scenarios"])
        primegov_text = next(fixture for fixture in manifest["fixtures"] if fixture["file"] == "worcester_primegov_city_council_agenda_74977.txt")
        self.assertEqual(primegov_text["source"], "real_document_extract")
        self.assertEqual(primegov_text["expected_classification"], "rendered")
        self.assertIn("spacy_entity_development", primegov_text["scenarios"])

    def test_worcester_primegov_agenda_fixture_has_pdf_and_extracted_text_for_entity_development(self) -> None:
        fixture_dir = CRAWLER_APP / "tests" / "fixtures" / "browser_crawler"
        pdf = fixture_dir / "worcester_primegov_city_council_agenda_74977.pdf"
        text = fixture_dir / "worcester_primegov_city_council_agenda_74977.txt"

        self.assertTrue(pdf.exists())
        self.assertTrue(text.exists())
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
        extracted = text.read_text(encoding="utf-8")
        self.assertGreater(len(extracted), 10_000)
        for expected in ["AGENDA OF THE CITY COUNCIL", "City Council Meeting", "Committee", "Councilor"]:
            with self.subTest(expected=expected):
                self.assertIn(expected, extracted)

    def test_massgov_browser_fixture_classifies_as_rendered_with_no_rate_limit_evidence(self) -> None:
        html = (CRAWLER_APP / "tests" / "fixtures" / "browser_crawler" / "massgov_city_town_websites.browser.html").read_text(encoding="utf-8")

        classification, evidence = classify_rendered_page_with_evidence(html, "Massachusetts city and town websites", title="Massachusetts city and town websites")

        self.assertEqual(classification, "rendered")
        self.assertEqual(evidence, [])

    def test_massgov_forbidden_fixture_reports_access_denied_evidence(self) -> None:
        html = (CRAWLER_APP / "tests" / "fixtures" / "browser_crawler" / "massgov_403.http.html").read_text(encoding="utf-8")

        classification, evidence = classify_rendered_page_with_evidence(html, "Mass.gov 403 This page is forbidden", title="Not allowed | Mass Gov")

        self.assertEqual(classification, "access_denied")
        self.assertEqual(evidence[0]["classification"], "access_denied")
        self.assertEqual(evidence[0]["source"], "visible_text")
        self.assertIn("forbidden", evidence[0]["signature"])

    def test_massgov_municipal_databank_fixture_classifies_as_rendered_and_extracts_links(self) -> None:
        html = (CRAWLER_APP / "tests" / "fixtures" / "browser_crawler" / "massgov_municipal_databank.browser.html").read_text(encoding="utf-8")

        classification, evidence = classify_rendered_page_with_evidence(
            html,
            "Division of Local Services Municipal Databank",
            title="Division of Local Services Municipal Databank | Mass.gov",
        )
        links, total_count, truncated = _extract_discovered_links(
            html,
            "https://www.mass.gov/info-details/division-of-local-services-municipal-databank",
            max_links=100,
        )

        self.assertEqual(classification, "rendered")
        self.assertEqual(evidence, [])
        self.assertEqual(total_count, 65)
        self.assertFalse(truncated)
        self.assertTrue(any("Community comparison report" in link.text for link in links))

    def test_massgov_browser_fixture_link_extraction_is_bounded_and_reports_truncation(self) -> None:
        html = (CRAWLER_APP / "tests" / "fixtures" / "browser_crawler" / "massgov_city_town_websites.browser.html").read_text(encoding="utf-8")

        links, total_count, truncated = _extract_discovered_links(
            html,
            "https://www.mass.gov/lists/massachusetts-city-and-town-websites",
            max_links=250,
        )

        self.assertEqual(len(links), 250)
        self.assertGreater(total_count, 250)
        self.assertTrue(truncated)
        self.assertTrue(any("Abington" in link.text for link in links))
        self.assertTrue(all(link.normalized_url.startswith(("http://", "https://")) for link in links))

    def test_link_extraction_handles_realistic_anchor_variants(self) -> None:
        html = """
        <HTML><body>
          <A HREF='/one' REL='next tag'><span>One</span></A>
          <a data-id="x" href="two.html">Two</a>
          <a href="mailto:test@example.gov">Mail</a>
          <a href="/one">Duplicate</a>
        </body></HTML>
        """

        links, total_count, truncated = _extract_discovered_links(html, "https://example.gov/base/index.html", max_links=10)

        self.assertFalse(truncated)
        self.assertEqual(total_count, 2)
        self.assertEqual([link.normalized_url for link in links], ["https://example.gov/one", "https://example.gov/base/two.html"])
        self.assertEqual(links[0].rel, ["next", "tag"])
        self.assertEqual(links[0].text, "One")

    def test_crawl_request_reports_frames_and_forms_and_scores_sparse_frameset_as_low_quality(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="text/html",
            body=(
            "<html><head><title>Legacy Assessors</title></head>"
            "<frameset rows='100,*'>"
            "<frame name='search' src='search-middle-ns.asp'>"
            "<frame name='results' src='home-bottom.asp'>"
            "</frameset>"
            "</html>"
            ).encode("utf-8"),
            elapsed_ms=3,
            final_url="https://assessor.example.gov/default.asp",
        )

        response = asyncio.run(
            crawl_request(
                {"url": "https://assessor.example.gov/default.asp", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}},
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
            )
        )

        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertLess(result["quality_score"], 0.5)
        self.assertEqual(
            result["diagnostics"]["frames"],
            [
                {
                    "url": "search-middle-ns.asp",
                    "normalized_url": "https://assessor.example.gov/search-middle-ns.asp",
                    "name": "search",
                    "title": "",
                    "source_url": "https://assessor.example.gov/default.asp",
                    "host": "assessor.example.gov",
                    "is_same_host": True,
                },
                {
                    "url": "home-bottom.asp",
                    "normalized_url": "https://assessor.example.gov/home-bottom.asp",
                    "name": "results",
                    "title": "",
                    "source_url": "https://assessor.example.gov/default.asp",
                    "host": "assessor.example.gov",
                    "is_same_host": True,
                },
            ],
        )
        self.assertTrue(result["diagnostics"]["content_metrics"]["has_frameset"])

    def test_crawl_request_reports_search_form_discovery_without_submitting_it(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="text/html",
            body=(
            "<html><body>"
            "<form method='POST' action='SearchResults.asp' name='frmSearch' id='frmSearch' target='bottom'>"
            "<input name='SearchStreetName' id='SearchStreetName' value=''>"
            "<input type='hidden' name='SearchSubmitted' value='yes'>"
            "<input type='submit' name='cmdGo' value='Go'>"
            "</form>"
            "</body></html>"
            ).encode("utf-8"),
            elapsed_ms=3,
            final_url="https://assessor.example.gov/search-middle-ns.asp",
        )

        response = asyncio.run(
            crawl_request(
                {"url": "https://assessor.example.gov/search-middle-ns.asp", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}},
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
            )
        )

        form = response["results"][0]["diagnostics"]["forms"][0]
        self.assertEqual(form["method"], "POST")
        self.assertEqual(form["action"], "SearchResults.asp")
        self.assertEqual(form["normalized_action"], "https://assessor.example.gov/SearchResults.asp")
        self.assertEqual(form["target"], "bottom")
        self.assertEqual(form["name"], "frmSearch")
        self.assertEqual(
            form["controls"],
            [
                {"tag": "input", "type": "text", "name": "SearchStreetName", "id": "SearchStreetName", "value": ""},
                {"tag": "input", "type": "hidden", "name": "SearchSubmitted", "id": "", "value": "yes"},
                {"tag": "input", "type": "submit", "name": "cmdGo", "id": "", "value": "Go"},
            ],
        )

    def test_classifier_marks_captcha_access_denied_login_paywall_and_rate_limit_pages(self) -> None:
        examples = {
            "captcha_detected": "Please complete the CAPTCHA to continue",
            "bot_challenge_detected": "Just a moment... Enable JavaScript and cookies to continue",
            "access_denied": "Access Denied. Your request has been blocked by security rules.",
            "login_required": "Sign in to continue to your account",
            "paywall_detected": "Subscribe now to continue reading this article",
            "rate_limited": "HTTP 429 Too Many Requests",
            "not_found": "Custom404 • Example, MA • CivicEngage",
            "site_error": "Runtime Error Server Error in '/' Application",
            "gone": "410 Gone",
            EXTRACTION_METHOD_BINARY_NO_TEXT: "",
        }

        for expected, text in examples.items():
            with self.subTest(expected=expected):
                self.assertEqual(classify_rendered_page("<html></html>", text, title=text), expected)

    def test_crawl_request_runs_direct_http_strategy_and_records_provenance(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=(
            '<html><body><main>Permit records</main>'
            '<a href="/permits" rel="tag next">Permits</a>'
            '<a href="https://other.example.gov/nope">Off host</a>'
            '</body></html>'
            ).encode("utf-8"),
            elapsed_ms=7,
            final_url="https://records.example.gov/search?q=permit",
        )

        response = asyncio.run(
            crawl_request(
                {
                    "url": "https://records.example.gov/search?q=permit",
                    "policy": {"strategies": [HTTP_DECLARED_CRAWLER], "limits": {"timeout_seconds": 5}},
                },
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
            )
        )

        result = response["results"][0]
        self.assertEqual(result["original_url"], "https://records.example.gov/search?q=permit")
        self.assertEqual(result["normalized_url"], "https://records.example.gov/search?q=permit")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["classification"], "rendered")
        self.assertEqual(result["selected_strategy"], HTTP_DECLARED_CRAWLER)
        self.assertEqual(result["attempted_strategies"][0]["strategy"], HTTP_DECLARED_CRAWLER)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["content_type"], "text/html; charset=utf-8")
        self.assertIn("Permit records", result["text"])
        self.assertEqual(result["extraction_method"], EXTRACTION_METHOD_HTTP_HTML)
        self.assertGreater(result["quality_score"], 0.8)
        self.assertEqual(
            result["discovered_links"],
            [
                {
                    "url": "/permits",
                    "normalized_url": "https://records.example.gov/permits",
                    "text": "Permits",
                    "rel": ["tag", "next"],
                    "source_url": "https://records.example.gov/search?q=permit",
                    "host": "records.example.gov",
                    "is_same_host": True,
                },
                {
                    "url": "https://other.example.gov/nope",
                    "normalized_url": "https://other.example.gov/nope",
                    "text": "Off host",
                    "rel": [],
                    "source_url": "https://records.example.gov/search?q=permit",
                    "host": "other.example.gov",
                    "is_same_host": False,
                },
            ],
        )

    def test_crawl_request_decompresses_gzip_http_body_before_extraction_and_links(self) -> None:
        import gzip

        html = (
            '<html><body><main>City websites</main>'
            '<a href="/abington">Abington</a>'
            '<a href="/abington">Abington duplicate</a>'
            '<a href="https://www.boston.gov">Boston</a>'
            '<a href="https://www.boston.gov/">Boston trailing slash</a>'
            '<a href="#section">Skip anchor</a>'
            '</body></html>'
        ).encode("utf-8")
        gz = gzip.compress(html)

        def fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"},
                body=gz,
                elapsed_ms=3,
            )

        response = asyncio.run(
            crawl_request(
                {
                    "url": "https://example.gov/cities",
                    "policy": {"strategies": [HTTP_DECLARED_CRAWLER], "limits": {"timeout_seconds": 5}},
                },
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
            )
        )
        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["classification"], "rendered")
        # The wire body was gzip-compressed; html/markdown must be decompressed, not raw gzip.
        self.assertNotIn("\x1f\x8b", result["html"])
        self.assertNotIn("\x1f\x8b", result["markdown"] or "")
        self.assertIn("City websites", result["text"])
        # Links are extracted from the decompressed HTML and duplicate normalized_urls collapse.
        normalized = [link["normalized_url"] for link in result["discovered_links"]]
        self.assertEqual(
            normalized,
            [
                "https://example.gov/abington",
                "https://www.boston.gov",
            ],
        )
        self.assertEqual(len(normalized), len(set(normalized)))
        self.assertEqual(result["discovered_link_count"], len(normalized))
        self.assertFalse(result["discovered_links_truncated"])

    def test_crawl_request_applies_request_compatibility_profile_and_cookie_policy(self) -> None:
        captured_headers: dict[str, str] = {}

        def fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
            captured_headers.update(headers)
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": "text/html"},
                body=b"<html><body><main>Permit records</main></body></html>",
                elapsed_ms=5,
            )

        response = asyncio.run(
            crawl_request(
                {
                    "url": "https://records.example.gov/search",
                    "policy": {
                        "strategies": [HTTP_CFFI_IMPERSONATE],
                        "request_profile": {
                            "user_agent_family": "desktop_chrome",
                            "accept_language": "en-US,en;q=0.9",
                            "cookie_jar": "per_host",
                            "cookies": {"records.example.gov": {"public_session": "abc123"}},
                        },
                    },
                },
                settings=CrawlerSettings(per_domain_delay_ms=0),
                cffi_fetcher=fetcher,
            )
        )

        result = response["results"][0]
        self.assertIn("Chrome/", captured_headers["User-Agent"])
        self.assertEqual(captured_headers["Accept-Language"], "en-US,en;q=0.9")
        self.assertIn("text/html", captured_headers["Accept"])
        self.assertEqual(captured_headers["Sec-Fetch-Site"], "cross-site")
        self.assertEqual(captured_headers["Cookie"], "public_session=abc123")
        self.assertEqual(result["diagnostics"]["request_profile"]["user_agent_family"], "desktop_chrome")
        self.assertEqual(result["diagnostics"]["request_profile"]["cookie_jar"], "per_host")

    def test_crawl_request_retries_transient_http_failures_and_reports_backoff(self) -> None:
        attempts = 0

        def fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("temporary timeout")
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": "text/html"},
                body=b"<html><body><main>Recovered permit records</main></body></html>",
                elapsed_ms=9,
            )

        response = asyncio.run(
            crawl_request(
                {
                    "url": "https://records.example.gov/search",
                    "policy": {"strategies": [HTTP_DECLARED_CRAWLER], "limits": {"max_retries": 1}},
                },
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
            )
        )

        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(attempts, 2)
        self.assertEqual(result["diagnostics"]["retry"]["attempts"], 2)
        self.assertEqual(result["diagnostics"]["retry"]["transient_errors"], ["temporary timeout"])

    def test_crawl_request_reports_retry_after_cooldown_for_rate_limits(self) -> None:
        def fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=429,
                headers={"content-type": "text/html", "retry-after": "120"},
                body=b"<html><body>Too Many Requests</body></html>",
                elapsed_ms=4,
            )

        response = asyncio.run(
            crawl_request(
                {"url": "https://records.example.gov/search", "policy": {"strategies": [HTTP_DECLARED_CRAWLER]}},
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
            )
        )

        result = response["results"][0]
        self.assertEqual(result["classification"], "rate_limited")
        self.assertEqual(result["diagnostics"]["cooldown"]["retry_after_seconds"], 120)
        self.assertEqual(result["diagnostics"]["cooldown"]["scope"], "domain")

    def test_crawl_request_reports_robots_sitemaps_feeds_and_completeness_metrics(self) -> None:
        def fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
            bodies = {
                "https://records.example.gov/robots.txt": b"User-agent: *\nCrawl-delay: 7\nSitemap: https://records.example.gov/sitemap.xml\n",
                "https://records.example.gov/sitemap.xml": b"<?xml version='1.0'?><urlset><url><loc>https://records.example.gov/a</loc></url></urlset>",
                "https://records.example.gov/feed.xml": b"<?xml version='1.0'?><rss><channel><title>Records</title></channel></rss>",
                "https://records.example.gov/search": (
                    b"<html><head>"
                    b"<link rel='alternate' type='application/rss+xml' href='/feed.xml'>"
                    b"</head><body><main>Permit records with enough content for scoring</main></body></html>"
                ),
            }
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": "text/html" if url.endswith("/search") else "text/plain"},
                body=bodies[url],
                elapsed_ms=3,
            )

        response = asyncio.run(
            crawl_request(
                {
                    "url": "https://records.example.gov/search",
                    "policy": {
                        "strategies": [HTTP_DECLARED_CRAWLER],
                        "discovery": {"robots": True, "sitemaps": True, "feeds": True},
                    },
                },
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
            )
        )

        result = response["results"][0]
        self.assertEqual(result["diagnostics"]["robots"]["crawl_delay_seconds"], 7)
        self.assertEqual(result["diagnostics"]["robots"]["sitemaps"], ["https://records.example.gov/sitemap.xml"])
        self.assertEqual(result["diagnostics"]["discovery"]["sitemap_urls"], ["https://records.example.gov/a"])
        self.assertEqual(result["diagnostics"]["discovery"]["feeds"], ["https://records.example.gov/feed.xml"])
        self.assertGreater(result["diagnostics"]["content_metrics"]["visible_text_length"], 20)
        self.assertGreater(result["quality_score"], 0.9)

    def test_crawl_request_passes_browser_state_profile_to_browser_worker(self) -> None:
        captured_options: list[RenderOptions] = []

        fetcher = make_fetcher(
            status_code=403,
            content_type="text/html",
            body=b"Access Denied",
            elapsed_ms=2,
        )

        def browser_worker(job: CrawlJob) -> CrawlResult:
            captured_options.append(job.options)
            return CrawlResult(
                url=job.url,
                status="success",
                classification="rendered",
                final_url=job.url,
                html="<html><body>Rendered records</body></html>",
                text="Rendered records",
                attempts=1,
                elapsed_ms=3,
                domain=job.domain,
            )

        response = asyncio.run(
            crawl_request(
                {
                    "url": "https://records.example.gov/search",
                    "policy": {
                        "strategies": [HTTP_DECLARED_CRAWLER, BROWSER_HEADLESS],
                        "browser_profile": {
                            "state": "per_host_warmed",
                            "viewport": "mobile",
                            "resource_mode": "reduced",
                            "cookie_jar": "per_host",
                        },
                    },
                },
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
                browser_worker=browser_worker,
            )
        )

        self.assertEqual(response["results"][0]["status"], "success")
        self.assertEqual(captured_options[0].browser_profile["state"], "per_host_warmed")
        self.assertEqual(captured_options[0].browser_profile["viewport"], "mobile")
        self.assertEqual(captured_options[0].browser_profile["resource_mode"], "reduced")

    def test_crawl_request_falls_back_to_browser_when_direct_http_is_blocked(self) -> None:
        browser_worker = make_browser_worker(
            html="<html><body>Rendered permits</body></html>",
            text="Rendered permits Link",
            elapsed_ms=5,
            metadata={"extraction_method": EXTRACTION_METHOD_DOM_TEXT},
        )
        assert_browser_fallback(self, browser_worker, "Rendered permits")


    def test_crawl_request_browser_strategy_handles_empty_rendered_content(self) -> None:
        browser_worker = make_browser_worker(html=None, text=None, elapsed_ms=5)
        result = assert_browser_fallback(self, browser_worker, "")
        self.assertIsNone(result["markdown"])

    def test_crawl_request_browser_strategy_returns_discovered_links_from_rendered_html(self) -> None:
        fetcher = make_fetcher(
            status_code=403,
            content_type="text/html",
            body=b"Access Denied",
            elapsed_ms=2,
        )

        browser_worker = make_browser_worker(
            final_url="https://records.example.gov/rendered",
            html='<html><body><a href="/rendered/details">Details</a></body></html>',
            text="Rendered details",
            elapsed_ms=11,
        )

        response = asyncio.run(
            crawl_request(
                {
                    "url": "https://records.example.gov/search",
                    "policy": {"strategies": [HTTP_DECLARED_CRAWLER, BROWSER_HEADLESS]},
                },
                settings=CrawlerSettings(per_domain_delay_ms=0),
                http_fetcher=fetcher,
                browser_worker=browser_worker,
            )
        )

        result = response["results"][0]
        self.assertEqual(
            result["discovered_links"],
            [
                {
                    "url": "/rendered/details",
                    "normalized_url": "https://records.example.gov/rendered/details",
                    "text": "Details",
                    "rel": [],
                    "source_url": "https://records.example.gov/rendered",
                    "host": "records.example.gov",
                    "is_same_host": True,
                }
            ],
        )

    def test_crawl_request_classifies_binary_archive_without_returning_decoded_body(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="application/octet-stream",
            body=b"PK\x03\x04\x14\x00\x00\x00binary zip bytes that should not be decoded",
            elapsed_ms=4,
        )

        response = run_crawl(
            "https://records.example.gov/archive.zip",
            {"strategies": [HTTP_DECLARED_CRAWLER]},
            http_fetcher=fetcher,
        )

        result = response["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["classification"], EXTRACTION_METHOD_BINARY_NO_TEXT)
        self.assertIsNone(result["selected_strategy"])
        self.assertIsNone(result["html"])
        self.assertIsNone(result["text"])
        self.assertEqual(result["quality_score"], 0.0)

    def test_crawl_request_uses_document_extract_for_pdf_bodies(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="application/pdf",
            body=b"%PDF-1.7 " + b"Sample agenda content " * 6,
            elapsed_ms=6,
        )

        response = run_crawl(
            "https://records.example.gov/agenda.pdf",
            {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": EXTRACTION_DOCUMENT},
            http_fetcher=fetcher,
        )

        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["selected_strategy"], HTTP_DECLARED_CRAWLER)
        self.assertEqual(result["content_type"], "application/pdf")
        self.assertTrue(result["text"].startswith("%PDF-1.7"))
        self.assertIsNone(result["html"])
        self.assertEqual(result["extraction_method"], EXTRACTION_METHOD_DOCUMENT)
        self.assertGreater(result["quality_score"], 0.8)

    def test_document_extract_formats_agenda_numbers_with_related_text(self) -> None:
        raw_text = "\n".join(
            [
                "5.",
                "",
                "APPROVAL OF THE MINUTES",
                "",
                "5a Adopt",
                "",
                "5a.",
                "",
                "6.",
                "",
                "Order - That the City Council approves the minutes.",
                "",
                "PUBLIC PARTICIPATION - a person may speak for no more than two minutes",
                "",
                "8.",
                "",
                "PETITIONS - a Petitioner may speak up to three minutes",
                "8a Refer to Public Works Committee",
                "8a.",
                "Councilor Rivera request sidewalk resurfacing on Lovell St.",
            ]
        )

        formatted = _format_extracted_document_text(raw_text, source_url="https://worcesterma.primegov.com/Public/CompiledDocument?meetingTemplateId=74977")

        self.assertIn("5. APPROVAL OF THE MINUTES", formatted)
        self.assertIn("5a. Order - That the City Council approves the minutes. [Action: Adopt]", formatted)
        self.assertNotIn("5a. 6. Order", formatted)
        self.assertNotIn("PUBLIC PARTICIPATION", formatted)
        self.assertIn("8. PETITIONS - a Petitioner may speak up to three minutes", formatted)
        self.assertIn("8a. Councilor Rivera request sidewalk resurfacing on Lovell St. [Action: Refer to Public Works Committee]", formatted)
        self.assertNotIn("5.\n\nAPPROVAL OF THE MINUTES", formatted)
        self.assertNotIn("5a Adopt\n\n5a.", formatted)

        formatted_again = _format_extracted_document_text("5a.\n\n6. Order - That the City Council approves the minutes.", source_url="https://worcesterma.primegov.com/Public/CompiledDocument?meetingTemplateId=74977")
        self.assertEqual(formatted_again, "5a. Order - That the City Council approves the minutes.")

    def test_document_extract_joins_wrapped_legal_notice_paragraphs(self) -> None:
        raw_text = "\n".join(
            [
                "Pursuant to MGL 7c. 30A sec.20(f), after notifying the chair of the public body",
                "",
                ",any person may make a video or audio recording of any open session of a",
                "meeting of a public body, or may transmit the meeting through any",
                "medium. At the beginning of the meeting, the chair should inform other",
                "attendees of any such recordings. Any member of the public wishing to",
                "speak at the meeting must receive permission of the chair. The listings of",
                "the agenda items are those reasonably anticipated by the chair which may",
                "be discussed at the meeting. Not all items may in fact be discussed and",
                "other items not listed may also be brought up for discussion to the extent",
                "permitted by law.",
            ]
        )

        formatted = _format_extracted_document_text(raw_text, source_url="https://www.townofgbma.gov/AgendaCenter/ViewFile/Agenda/_05272026-2389")

        self.assertEqual(
            formatted,
            "Pursuant to MGL 7c. 30A sec.20(f), after notifying the chair of the public body, "
            "any person may make a video or audio recording of any open session of a meeting of a public body, "
            "or may transmit the meeting through any medium. At the beginning of the meeting, the chair should "
            "inform other attendees of any such recordings. Any member of the public wishing to speak at the "
            "meeting must receive permission of the chair. The listings of the agenda items are those reasonably "
            "anticipated by the chair which may be discussed at the meeting. Not all items may in fact be discussed "
            "and other items not listed may also be brought up for discussion to the extent permitted by law.",
        )

    def test_crawl_request_formats_document_extract_text_before_returning_it(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="text/plain; charset=utf-8",
            body="5a.\n\n6. Order - That the City Council approves the minutes.".encode("utf-8"),
            elapsed_ms=6,
        )

        response = run_crawl(
            "https://records.example.gov/notice.txt",
            {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": EXTRACTION_DOCUMENT},
            http_fetcher=fetcher,
        )

        result = response["results"][0]
        self.assertEqual(result["text"], "5a. Order - That the City Council approves the minutes.")

    def test_crawl_request_document_extract_uses_markitdown_for_documents(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="text/csv",
            body=b"name,age\nAlice,30\nBob,25",
            elapsed_ms=3,
        )

        response = run_crawl(
            "https://records.example.gov/data.csv",
            {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": EXTRACTION_DOCUMENT},
            http_fetcher=fetcher,
        )

        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertIn("Alice", result["text"])
        self.assertEqual(result["extraction_method"], EXTRACTION_METHOD_DOCUMENT)

    def test_crawl_request_default_document_extractor_uses_text_bodies(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="text/plain; charset=utf-8",
            body="Posted notice text".encode("utf-8"),
            elapsed_ms=3,
        )

        response = run_crawl(
            "https://records.example.gov/notice.txt",
            {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": EXTRACTION_DOCUMENT},
            http_fetcher=fetcher,
        )

        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["text"], "Posted notice text")
        self.assertEqual(result["extraction_method"], EXTRACTION_METHOD_DOCUMENT)

    def test_crawl_request_uses_trafilatura_extractor_for_noisy_html(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="text/html",
            body=b"<html><nav>Menu</nav><main>Permit detail record text</main></html>",
            elapsed_ms=4,
        )

        response = run_crawl(
            "https://records.example.gov/detail",
            {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": EXTRACTION_TRAFILATURA},
            http_fetcher=fetcher,
        )

        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["selected_strategy"], HTTP_DECLARED_CRAWLER)
        self.assertIn("Permit detail record text", result["text"])
        self.assertEqual(result["extraction_method"], EXTRACTION_METHOD_TRAFILATURA)
        self.assertGreater(result["quality_score"], 0.8)

    def test_trafilatura_strategy_does_not_parse_pdf_bodies(self) -> None:
        fetcher = make_fetcher(
            status_code=200,
            content_type="application/pdf",
            body=b"%PDF-1.7 binary pdf bytes",
            elapsed_ms=5,
        )

        response = run_crawl(
            "https://records.example.gov/agenda.pdf",
            {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": EXTRACTION_TRAFILATURA},
            http_fetcher=fetcher,
        )

        result = response["results"][0]
        self.assertEqual(result["extraction_method"], EXTRACTION_METHOD_TRAFILATURA)
        self.assertEqual(result["classification"], EXTRACTION_METHOD_BINARY_NO_TEXT)
        self.assertEqual(result["discovered_links"], [])

    def test_crawl_request_browser_extraction_can_force_document_pipeline(self) -> None:
        # Extraction is orthogonal to the fetch strategy: BROWSER_HEADLESS paired
        # with the document extraction pipeline yields a document_extract result.
        body_html = "<html><body><nav>Menu</nav><main>Permit detail record text</main></body></html>"
        response = run_crawl(
            "https://records.example.gov/detail",
            {"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_DOCUMENT},
            browser_worker=make_browser_worker(html=body_html),
        )
        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["selected_strategy"], BROWSER_HEADLESS)
        self.assertEqual(result["extraction_method"], EXTRACTION_METHOD_DOCUMENT)
        self.assertIn("Permit detail record text", result["text"])

    def test_crawl_request_browser_extraction_can_force_trafilura_pipeline(self) -> None:
        body_html = "<html><body><nav>Menu</nav><main>Permit detail record text</main></body></html>"
        response = run_crawl(
            "https://records.example.gov/detail",
            {"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_TRAFILATURA},
            browser_worker=make_browser_worker(html=body_html),
        )
        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["selected_strategy"], BROWSER_HEADLESS)
        self.assertEqual(result["extraction_method"], EXTRACTION_METHOD_TRAFILATURA)
        self.assertIn("Permit detail record text", result["text"])

    def test_crawl_request_rejects_unknown_strategy_before_execution(self) -> None:
        with self.assertRaises(ValueError) as context:
            asyncio.run(
                crawl_request(
                    {
                        "url": "https://records.example.gov/search",
                        "policy": {"strategies": ["UNKNOWN_STRATEGY"]},
                    },
                    settings=CrawlerSettings(per_domain_delay_ms=0),
                    http_fetcher=lambda url, headers, timeout_seconds: None,
                )
            )

        self.assertEqual(str(context.exception), "invalid_policy_strategy")

    def test_scheduler_runs_different_domains_concurrently_and_preserves_result_order(self) -> None:
        events: list[tuple[str, str]] = []
        release = asyncio.Event()
        jobs = [
            CrawlJob(index=0, url="https://a.example.gov/slow", domain="example.gov", options=RenderOptions()),
            CrawlJob(index=1, url="https://b.example.org/fast", domain="example.org", options=RenderOptions()),
        ]

        async def worker(job: CrawlJob) -> CrawlResult:
            events.append(("start", job.domain))
            if job.index == 0:
                await release.wait()
            else:
                release.set()
            events.append(("finish", job.domain))
            return CrawlResult(
                url=job.url,
                status="success",
                classification="rendered",
                final_url=job.url,
                html=f"<html>{job.domain}</html>",
                text=job.domain,
                attempts=1,
                elapsed_ms=1,
                domain=job.domain,
            )

        results = asyncio.run(DomainScheduler(global_limit=2, per_domain_limit=1, delay_seconds=0).run(jobs, worker))

        self.assertEqual([result.url for result in results], [job.url for job in jobs])
        self.assertEqual(events[:2], [("start", "example.gov"), ("start", "example.org")])

    def test_scheduler_set_domain_cooldown_applies_wait(self) -> None:
        run_cooldown_case(self, cooldown=0.1, expect_wait=True, delay_seconds=0.01)

    def test_scheduler_set_domain_cooldown_zero_does_not_wait(self) -> None:
        run_cooldown_case(self, cooldown=0, expect_wait=False)

    def test_scheduler_cooldown_expires_before_job(self) -> None:
        run_cooldown_case(self, cooldown=0, expect_wait=False, past=True)

    def test_browser_renderer_extracts_rendered_html_and_text_with_fake_browser(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        # Mock Camoufox AsyncBrowser
        mock_page = AsyncMock()
        mock_page.url = "https://records.example.gov/final"
        mock_page.evaluate = AsyncMock(side_effect=lambda script: "Rendered Title" if "document.title" in script else "complete")
        mock_page.content = AsyncMock(return_value="<html><body><main>Rendered HTML</main></body></html>")
        mock_page.goto = AsyncMock()

        renderer = make_renderer(mock_page)

        import asyncio
        result = asyncio.run(renderer.render("https://records.example.gov/search", RenderOptions(timeout_seconds=7)))

        self.assertEqual(result.status, "success")
        self.assertEqual(result.classification, "rendered")
        self.assertEqual(result.final_url, "https://records.example.gov/final")
        self.assertEqual(result.html, "<html><body><main>Rendered HTML</main></body></html>")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.domain, "example.gov")
        self.assertEqual(result.metadata["title"], "Rendered Title")

    def test_browser_renderer_retries_transient_failures_before_success(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        attempts = 0

        async def mock_goto(url: str, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("navigation timed out")

        mock_page = AsyncMock()
        mock_page.url = "https://records.example.gov/final"
        mock_page.goto = mock_goto
        mock_page.evaluate = AsyncMock(side_effect=lambda script: "Rendered Title" if "document.title" in script else "complete")
        mock_page.content = AsyncMock(return_value="<html><body>ok</body></html>")

        renderer = make_renderer(mock_page)

        import asyncio
        result = asyncio.run(renderer.render("https://records.example.gov/search", RenderOptions(max_retries=1)))

        # The retry happens in the outer render() method; _render_with_browser always returns attempts=1
        # The test verifies that the retry logic is invoked (2 goto calls) and succeeds
        self.assertEqual(result.status, "success")
        self.assertEqual(attempts, 2)

    def test_browser_renderer_returns_blocked_result_for_captcha_page(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        mock_page = AsyncMock()
        mock_page.url = "https://records.example.gov/check"
        mock_page.goto = AsyncMock()
        mock_page.evaluate = AsyncMock(side_effect=lambda script: "Checking your browser" if "document.title" in script else ("complete" if "document.readyState" in script else ""))
        mock_page.content = AsyncMock(return_value="<html><body>captcha required</body></html>")

        renderer = make_renderer(mock_page)

        import asyncio
        result = asyncio.run(renderer.render("https://records.example.gov/search", RenderOptions(max_retries=2)))

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.classification, "captcha_detected")
        self.assertEqual(result.attempts, 1)

    def test_browser_renderer_waits_for_cloudflare_challenge_to_resolve(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        # Track call sequence
        call_seq = {"content": 0, "evaluate_title": 0, "evaluate_text": 0, "evaluate_ready": 0}

        async def mock_evaluate(script: str):
            if "document.readyState" in script:
                call_seq["evaluate_ready"] += 1
                return "complete"
            if "document.title" in script:
                call_seq["evaluate_title"] += 1
                if call_seq["evaluate_title"] >= 2:
                    return "Resolved Content"
                return "Just a moment..."
            if "document.body" in script:
                call_seq["evaluate_text"] += 1
                if call_seq["evaluate_text"] >= 2:
                    return "Resolved Text"
                return "Enable JavaScript and cookies to continue"
            return ""

        async def mock_content():
            call_seq["content"] += 1
            if call_seq["content"] >= 2:
                return "<html><body><main>Resolved HTML</main></body></html>"
            return "<html><title>Just a moment...</title><body>Enable JavaScript and cookies to continue</body></html>"

        mock_page = AsyncMock()
        mock_page.url = "https://records.example.gov/final"
        mock_page.goto = AsyncMock()
        mock_page.evaluate = mock_evaluate
        mock_page.content = mock_content

        renderer = make_renderer(mock_page, challenge_wait_seconds=2)

        import asyncio
        result = asyncio.run(renderer.render("https://records.example.gov/search", RenderOptions()))

        self.assertEqual(result.status, "success")
        self.assertEqual(result.classification, "rendered")
        self.assertEqual(result.text, "Resolved Text")
        self.assertEqual(result.metadata["challenge_resolved"], True)

    def test_browser_renderer_returns_blocked_when_challenge_does_not_resolve(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        extract_count = 0

        async def mock_evaluate(script: str):
            nonlocal extract_count
            if "document.readyState" in script:
                return "complete"
            if "outerHTML" in script:
                return "<html><title>Just a moment...</title><body>Enable JavaScript and cookies to continue</body></html>"
            if "innerText" in script:
                return "Enable JavaScript and cookies to continue"
            if "document.title" in script:
                return "Just a moment..."
            return ""

        mock_page = AsyncMock()
        mock_page.url = "https://records.example.gov/check"
        mock_page.goto = AsyncMock()
        mock_page.evaluate = mock_evaluate
        mock_page.content = AsyncMock(return_value="<html><title>Just a moment...</title><body>Enable JavaScript and cookies to continue</body></html>")

        renderer = make_renderer(mock_page, challenge_wait_seconds=0)

        import asyncio
        result = asyncio.run(renderer.render("https://records.example.gov/search", RenderOptions()))

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.classification, "bot_challenge_detected")
        self.assertEqual(result.metadata["challenge_resolved"], False)

    def test_browser_renderer_uses_pdf_text_extractor_for_pdf_urls(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        mock_page = AsyncMock()
        mock_page.url = "https://records.example.gov/agenda.pdf"
        mock_page.goto = AsyncMock()

        async def mock_evaluate(script: str):
            if "document.title" in script:
                return "PDF Title"
            if "document.body" in script:
                return ""  # Empty body text for PDF
            return "complete"

        mock_page.evaluate = mock_evaluate
        mock_page.content = AsyncMock(return_value="")

        renderer = make_renderer(mock_page, pdf_text_extractor=lambda url: "PDF agenda text",)

        import asyncio
        result = asyncio.run(renderer.render("https://records.example.gov/agenda.pdf", RenderOptions()))

        self.assertEqual(result.status, "success")
        self.assertEqual(result.classification, "rendered")
        self.assertEqual(result.text, "PDF agenda text")
        self.assertEqual(result.metadata["extraction_method"], "pdf_text")

    def test_browser_renderer_navigates_to_warmup_url_before_target(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        visited = []

        mock_page = AsyncMock()
        mock_page.url = "https://records.example.gov/page"
        mock_page.goto = AsyncMock(side_effect=lambda url, **kwargs: visited.append(url))
        mock_page.evaluate = AsyncMock(side_effect=lambda script: "Rendered" if "document.title" in script else "complete")
        mock_page.content = AsyncMock(return_value="<html><body>ok</body></html>")

        renderer = make_renderer(mock_page, warmup_urls=["https://records.example.gov/"])

        import asyncio
        result = asyncio.run(renderer.render("https://records.example.gov/page", RenderOptions()))

        self.assertEqual(result.status, "success")
        self.assertEqual(visited, ["https://records.example.gov/", "https://records.example.gov/page"])

    def test_settings_default_log_level_is_uvicorn_compatible_lowercase_info(self) -> None:
        settings = CrawlerSettings.from_mapping({})

        self.assertEqual(settings.log_level, "info")

    def test_settings_load_environment_overrides(self) -> None:
        settings = CrawlerSettings.from_mapping(
            {
                "BROWSER_CRAWLER_GLOBAL_CONCURRENCY": "8",
                "BROWSER_CRAWLER_PER_DOMAIN_DELAY_MS": "2500",
                "BROWSER_CRAWLER_RESPECT_ROBOTS": "false",
            }
        )

        self.assertEqual(settings.global_concurrency, 8)
        self.assertEqual(settings.per_domain_delay_ms, 2500)
        self.assertFalse(settings.respect_robots)

if __name__ == "__main__":
    unittest.main()
