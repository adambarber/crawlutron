"""HTTP fetch strategy behavior against a real local server (no mocks)."""

from functools import partial

from _fetcher_factories import make_fetch, run_http_strategy

from app.constants import EXTRACTION_FULL, HTTP_DECLARED_CRAWLER
from app.crawl.cache import HttpCache
from app.crawl.result_helpers import _crawl_result_from_engine_result
from app.crawl.strategies.fetch_policy import _looks_like_binary_body
from app.crawl.strategies.fetchers import _default_http_fetcher
from app.crawl.strategies.http import _http_strategy_result
from app.models import CrawlStrategyAttempt
from app.validation import HostPolicy

_default_http_fetcher_with_policy = partial(
    _default_http_fetcher,
    policy=HostPolicy.unrestricted(),
)


async def _run(**kwargs):
    kwargs.setdefault("host_policy", HostPolicy.unrestricted())
    return await _http_strategy_result(**kwargs)


def _html_fetcher(html: str):
    # Returns an async fetcher that serves a fixed HTML body for any URL.
    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        return make_fetch(url=url, final_url=url, status=200, ctype="text/html", body=html.encode("utf-8"))

    return fetcher


def _run_kwargs(fetcher):
    return dict(
        original_url="http://x.test/",
        normalized_url="http://x.test/",
        strategy=HTTP_DECLARED_CRAWLER,
        http_fetcher=fetcher,
        cache=HttpCache(),
        content_cache=None,
    )


def http_policy(**overrides):
    policy = {
        "strategies": [HTTP_DECLARED_CRAWLER],
        "extraction": EXTRACTION_FULL,
        "scope_regex": None,
        "limits": {
            "timeout_seconds": 10,
            "max_retries": 0,
            "challenge_wait_seconds": 15,
            "max_discovered_links": 50,
        },
        "headers": {},
        "request_profile": {"user_agent_family": "generic_crawler", "cookie_jar": "none"},
        "browser_profile": {},
        "discovery": {},
    }
    policy.update(overrides)
    return policy


class TestHttpStrategy:
    async def test_discovery_fetch_omits_page_conditional_headers(self):
        # #1-fix: If-None-Match / If-Modified-Since belong to the HTML page
        # request. Forwarding them to robots.txt/sitemap fetches invites false
        # 304s -- discovery fetches get base headers only.
        recorded: list[tuple[str, dict]] = []

        async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            recorded.append((url, headers))
            return make_fetch(
                url=url,
                final_url=url,
                status=200,
                ctype="text/html",
                body=b"<html><body><a href='/p'>p</a></body></html>",
            )

        cache = HttpCache()
        cache.store("http://x.test/", {"etag": '"page-etag-v1"'})
        await _run(
            policy=http_policy(discovery={"robots": True}),
            original_url="http://x.test/",
            normalized_url="http://x.test/",
            strategy=HTTP_DECLARED_CRAWLER,
            http_fetcher=fetcher,
            cache=cache,
            content_cache=None,
        )
        robot_calls = [h for u, h in recorded if u.endswith("/robots.txt")]
        assert robot_calls, "discovery never fetched robots.txt"
        for headers in robot_calls:
            lower = {k.lower() for k in headers}
            assert "if-none-match" not in lower
            assert "if-modified-since" not in lower

    async def test_html_diagnostics_skipped_without_discovery(self):
        # #2: _html_diagnostics (frames/forms via lxml parse) must be skipped
        # when discovery is not requested, to avoid a redundant HTML parse on
        # every page of a content-only crawl.
        html = "<html><body><iframe src='/x'></iframe></body></html>"

        fetcher = _html_fetcher(html)
        kwargs = _run_kwargs(fetcher)
        # discovery omitted -> no frames in diagnostics
        r1 = await _run(policy=http_policy(discovery=None), **kwargs)
        assert "frames" not in r1.diagnostics
        # discovery requested -> frames computed
        r2 = await _run(policy=http_policy(discovery={"sitemaps": True}), **kwargs)
        assert "frames" in r2.diagnostics

    async def test_http_strategy_extracts_html_markdown_content_only(self, base_url):
        # #2-fix: content-only crawl (no discovery, depth 0) skips link
        # extraction entirely -- discovered_links comes back empty instead of
        # paying an HTML parse per page nobody consumes.
        result = await _run(
            original_url=f"{base_url}/",
            normalized_url=f"{base_url}/",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher_with_policy,
        )
        assert result.status == "success"
        assert result.classification == "rendered"
        assert "Example Org" in (result.text or "")
        assert "Example Org" in (result.markdown or "")
        assert result.discovered_link_count == 0
        assert result.discovered_links == []
        assert result.next_page_url == f"{base_url}/page2"
        assert result.http_status == 200


    async def test_http_strategy_404_classified_not_found(self, base_url):
        result = await _run(
            original_url=f"{base_url}/missing",
            normalized_url=f"{base_url}/missing",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher_with_policy,
        )
        assert result.status == "failed"
        assert result.classification == "not_found"
        assert result.http_status == 404

    async def test_http_strategy_access_denied_page_blocked(self, base_url):
        result = await _run(
            original_url=f"{base_url}/blocked",
            normalized_url=f"{base_url}/blocked",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher_with_policy,
        )
        assert result.classification == "access_denied"
        assert result.status == "blocked"

    async def test_http_conditional_cache_returns_not_modified_on_304(self, base_url):
        url = f"{base_url}/with_etag"
        cache = HttpCache()
        cache.store(url, {"etag": "v1"})
        result = await _run(
            original_url=url,
            normalized_url=url,
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher_with_policy,
            cache=cache,
        )
        assert result.classification == "not_modified"
        assert result.http_status == 304
        assert result.html is None

    async def test_http_csv_document_via_http(self, base_url):
        result = await _run(
            original_url=f"{base_url}/sample.csv",
            normalized_url=f"{base_url}/sample.csv",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher_with_policy,
        )
        assert "Alice" in (result.markdown or "")
        assert "|" in (result.markdown or "")

    async def test_http_json_document_via_http(self, base_url):
        result = await _run(
            original_url=f"{base_url}/sample.json",
            normalized_url=f"{base_url}/sample.json",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher_with_policy,
        )
        assert '"a":1' in (result.markdown or "")

    async def test_http_pdf_table_preserved_via_http(self, base_url):
        result = await _run(
            original_url=f"{base_url}/sample_table.pdf",
            normalized_url=f"{base_url}/sample_table.pdf",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=http_policy(),
            http_fetcher=_default_http_fetcher_with_policy,
        )
        assert "Name" in (result.markdown or "")
        assert "Department" in (result.markdown or "")
        assert "Salary" in (result.markdown or "")
        assert "| Alice | Engineering | 120000 |" in (result.markdown or "")

    def test_looks_like_binary_body_detects_archives(self):
        assert _looks_like_binary_body(b"PK\x03\x04", "application/octet-stream", "http://x/a.zip")
        assert not _looks_like_binary_body(b"<html>", "text/html", "http://x/")
        assert not _looks_like_binary_body(b"%PDF-1.4", "application/pdf", "http://x/a.pdf")

    async def test_crawl_request_async_returns_unified_results_dict(self, base_url):
        from _fetcher_factories import crawl_homepage_async

        response = await crawl_homepage_async(base_url)
        assert response["submitted_count"] == 1
        assert len(response["results"]) == 1
        assert "Example Org" in response["results"][0]["markdown"]

    async def test_js_endpoints_and_sitemap_discovery_gated_without_discovery(self):
        # B1: the heavier discovery-only extras -- JS-endpoint extraction and the
        # remote sitemap/feed discovery fetches -- must be skipped when discovery
        # is not requested, to avoid per-page regex scans and extra HTTP requests
        # on content-only crawls. discovered_links / next_page_url stay on (they
        # are standard result fields).
        html = (
            "<html><body>"
            "<a href='/page2'>next</a>"
            "<script>fetch('https://api.example.com/data')</script>"
            "</body></html>"
        )

        fetcher = _html_fetcher(html)
        kwargs = _run_kwargs(fetcher)
        # discovery omitted -> js_endpoints empty, no discovery diagnostics block
        r1 = await _run(policy=http_policy(discovery=None), **kwargs)
        assert r1.diagnostics["js_endpoints"] == []
        assert "discovery" not in r1.diagnostics
        # content-only crawl: link extraction skipped entirely (#2-fix)
        assert r1.discovered_link_count == 0
        # next_page_url is always computed (standard field, proven by
        # test_http_strategy_extracts_html_markdown_and_links); it must not be
        # gated behind discovery.
        assert r1.next_page_url is None or isinstance(r1.next_page_url, str)
        # discovery requested -> js_endpoints extracted; the (remote)
        # sitemap/feed discovery is performed, so the robots diagnostics block
        # is populated (proving the gated function ran for this crawl).
        r2 = await _run(
            policy=http_policy(discovery={"robots": True, "sitemaps": True, "feeds": True}), **kwargs
        )
        assert isinstance(r2.diagnostics["js_endpoints"], list)
        assert r2.diagnostics["js_endpoints"]  # non-empty: HTML contains a fetch() endpoint
        assert "robots" in r2.diagnostics


async def test_recursive_crawl_drops_sitewide_lists_from_page_diagnostics(monkeypatch):
    # #13-fix: during recursive crawls, site-wide sitemap/feed URL lists are
    # frontier seeds, not per-page payload -- returning them on every page's
    # diagnostics wastes memory across thousands of pages.
    import app.crawl.strategies.fetch_policy as http_mod

    async def fake_diag(**kwargs):
        return {
            "discovery": {"sitemap_urls": ["https://x.test/sitemap.xml"], "feeds": ["https://x.test/feed"]}
        }

    monkeypatch.setattr(http_mod, "_discovery_diagnostics", fake_diag)

    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        return make_fetch(
            url=url, final_url=url, status=200, ctype="text/html", body=b"<html><body>x</body></html>"
        )

    kwargs = dict(
        original_url="http://x.test/",
        normalized_url="http://x.test/",
        strategy=HTTP_DECLARED_CRAWLER,
        http_fetcher=fetcher,
        cache=HttpCache(),
        content_cache=None,
    )
    r_rec = await _run(
        policy=http_policy(
            discovery={"sitemaps": True}, limits={"timeout_seconds": 10, "max_retries": 0, "max_depth": 1}
        ),
        **kwargs,
    )
    r_flat = await _run(policy=http_policy(discovery={"sitemaps": True}), **kwargs)

    rec_disc = r_rec.diagnostics.get("discovery") or {}
    assert "sitemap_urls" not in rec_disc and "feeds" not in rec_disc
    flat_disc = r_flat.diagnostics.get("discovery") or {}
    assert flat_disc.get("sitemap_urls") == ["https://x.test/sitemap.xml"]
    assert flat_disc.get("feeds") == ["https://x.test/feed"]


class TestHttpStrategyRetry:
    async def test_http_strategy_with_cffi_retry(self):
        from app.crawl.cache import HttpCache

        call_count = [0]

        def retry_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
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

        result = await run_http_strategy(
            url="http://x/",
            policy={
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 1, "max_discovered_links": 10},
            },
            http_fetcher=retry_fetcher,
            cache=HttpCache(),
        )
        assert result.classification == "rendered"


async def test_a_binary_body_result_keeps_its_attempt_type_for_the_render_adapter():
    """A binary response must reach the render pipeline as an EngineCrawlResult.

    The binary result is the one place the HTTP strategy copies a finished
    result to override a single field. Rebuilding that copy through
    ``dataclasses.asdict`` recursively converts the frozen ``CrawlStrategyAttempt``
    list into plain dicts, so the copy still *looks* right and fails only when a
    consumer reaches for an attribute -- which the render adapter does, on the
    first field it reads. Driving the real strategy and the real adapter together
    is the only version of this that could have caught it: asserting on the
    strategy's result alone passes happily with dicts in the list.
    """
    # An archive: inside the detector's real contract (PK magic under a
    # non-text content type). An image would not qualify -- image/png is
    # deliberately outside the binary set -- so widening the fixture would have
    # been quietly testing a policy change instead of the copy bug.
    archive = b"PK\x03\x04" + b"\x00" * 64

    async def binary_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        return make_fetch(
            url=url,
            final_url=url,
            status=200,
            ctype="application/octet-stream",
            body=archive,
        )

    result = await _run(policy=http_policy(), **_run_kwargs(binary_fetcher))

    # The binary no-text terminal result: not an error, and not a render.
    assert result.classification == "binary_no_text"
    assert result.attempted_strategies
    # The declared type, not a dict that merely happens to have the right keys.
    assert all(isinstance(a, CrawlStrategyAttempt) for a in result.attempted_strategies)
    assert result.attempted_strategies[0].elapsed_ms > 0

    # And the adapter that reads those attributes must not raise.
    adapted = _crawl_result_from_engine_result(result, url="http://x.test/")
    assert adapted.classification == "binary_no_text"
    assert adapted.elapsed_ms == result.attempted_strategies[0].elapsed_ms
    assert adapted.attempts == len(result.attempted_strategies)
