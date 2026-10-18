"""HTTP fetch strategy behavior against a real local server (no mocks)."""

from app.models.crawl_result import CrawlStrategyAttempt
from app.orchestration.results import crawl_result_from_engine_result
from app.transport.cache import HttpCache
from app.transport.fetch_policy import looks_like_binary_body
from app.transport.fetchers import default_http_fetcher
from app.transport.http import http_strategy_result
from app.vocabulary.extraction import EXTRACTION_FULL
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._assertions import assert_result_success
from tests._fetcher_factories import (
    request_fetcher,
    run_http_strategy,
    scripted_fetcher,
    sequenced_fetcher,
    strategy_request,
)
from tests._policy_factories import canonical


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


async def _run(
    *,
    url,
    strategy=HTTP_DECLARED_CRAWLER,
    policy: dict | None = None,
    http_fetcher=default_http_fetcher,
    **kwargs,
):
    """Drive the real ``http_strategy_result`` for one page.

    Page identity is one argument (no misspelling five params); the strategy,
    policy and fetcher default to the single "declared strategy" combination.
    """
    return await http_strategy_result(
        request=strategy_request(url, http_policy() if policy is None else policy, strategy),
        http_fetcher=http_fetcher,
        **kwargs,
    )


def _html_fetcher(html: str):
    """An async fetcher that serves a fixed HTML body for any URL."""
    return scripted_fetcher(status=200, ctype="text/html", body=html.encode("utf-8"))


def _run_kwargs(fetcher):
    return dict(
        url="http://x.test/",
        http_fetcher=fetcher,
        cache=HttpCache(),
        content_cache=None,
    )


class TestHttpStrategy:
    async def test_discovery_fetch_omits_page_conditional_headers(self):
        # If-None-Match / If-Modified-Since belong to the HTML page request. Forwarding them to
        # robots.txt/sitemap fetches invites false 304s -- discovery fetches get base headers only.
        recorded: list[tuple[str, dict]] = []

        def respond(url, headers):
            recorded.append((url, headers))
            return (200, b"<html><body><a href='/p'>p</a></body></html>", "text/html")

        fetcher = request_fetcher(respond)

        cache = HttpCache()
        cache.store("http://x.test/", {"etag": '"page-etag-v1"'})
        await _run(
            policy=http_policy(discovery={"robots": True}),
            url="http://x.test/",
            http_fetcher=fetcher,
            cache=cache,
            content_cache=None,
        )
        robot_calls = [h for u, h in recorded if u.raw.endswith("/robots.txt")]
        assert robot_calls, "discovery never fetched robots.txt"
        for headers in robot_calls:
            lower = {k.lower() for k in headers}
            assert "if-none-match" not in lower
            assert "if-modified-since" not in lower

    async def test_html_diagnostics_skipped_without_discovery(self):
        # html_diagnostics (frames/forms via lxml parse) must be skipped when discovery is not requested, to
        # avoid a redundant HTML parse on every page of a content-only crawl.
        html = "<html><body><iframe src='/x'></iframe></body></html>"

        fetcher = _html_fetcher(html)
        kwargs = _run_kwargs(fetcher)
        # Discovery omitted -> no frames in diagnostics
        r1 = await _run(policy=http_policy(discovery=None), **kwargs)
        assert "frames" not in r1.diagnostics
        # Discovery requested -> frames computed
        r2 = await _run(policy=http_policy(discovery={"sitemaps": True}), **kwargs)
        assert "frames" in r2.diagnostics

    async def test_http_strategy_extracts_html_markdown_content_only(self, base_url):
        result = await _run(
            url=f"{base_url}/",
        )
        assert_result_success(result)
        assert "Example Org" in (result.text or "")
        assert "Example Org" in (result.markdown or "")
        assert result.discovered_link_count == 0
        assert result.discovered_links == []
        assert result.next_page_url == canonical(f"{base_url}/page2")
        assert result.http_status == 200

    async def test_http_strategy_404_classified_not_found(self, base_url):
        result = await _run(
            url=f"{base_url}/missing",
        )
        assert result.status == "failed"
        assert result.classification == "not_found"
        assert result.http_status == 404

    async def test_http_strategy_access_denied_page_blocked(self, base_url):
        result = await _run(
            url=f"{base_url}/blocked",
        )
        assert result.classification == "access_denied"
        assert result.status == "blocked"

    async def test_http_conditional_cache_returns_not_modified_on_304(self, base_url):
        url = f"{base_url}/with_etag"
        cache = HttpCache()
        cache.store(canonical(url), {"etag": "v1"})
        result = await _run(
            url=url,
            cache=cache,
        )
        assert result.classification == "not_modified"
        assert result.http_status == 304
        assert result.html is None

    async def test_http_csv_document_via_http(self, base_url):
        result = await _run(
            url=f"{base_url}/sample.csv",
        )
        assert "Alice" in (result.markdown or "")
        assert "|" in (result.markdown or "")

    async def test_http_json_document_via_http(self, base_url):
        result = await _run(
            url=f"{base_url}/sample.json",
        )
        assert '"a":1' in (result.markdown or "")

    async def test_http_pdf_table_preserved_via_http(self, base_url):
        result = await _run(
            url=f"{base_url}/sample_table.pdf",
        )
        assert "Name" in (result.markdown or "")
        assert "Department" in (result.markdown or "")
        assert "Salary" in (result.markdown or "")
        assert "| Alice | Engineering | 120000 |" in (result.markdown or "")

    def test_looks_like_binary_body_detects_archives(self):
        assert looks_like_binary_body(b"PK\x03\x04", "application/octet-stream", canonical("http://x/a.zip"))
        assert not looks_like_binary_body(b"<html>", "text/html", canonical("http://x/"))
        assert not looks_like_binary_body(b"%PDF-1.4", "application/pdf", canonical("http://x/a.pdf"))

    async def test_crawl_request_async_returns_unified_results_dict(self, base_url):
        from tests._fetcher_factories import crawl_homepage_async

        response = await crawl_homepage_async(base_url)
        assert response["submitted_count"] == 1
        assert len(response["results"]) == 1
        assert "Example Org" in response["results"][0].markdown

    async def test_js_endpoints_and_sitemap_discovery_gated_without_discovery(self):
        # The heavier discovery-only extras -- JS-endpoint extraction and the remote sitemap/feed discovery
        # fetches -- must be skipped when discovery is not requested, to avoid per-page regex scans and extra
        html = (
            "<html><body>"
            "<a href='/page2'>next</a>"
            "<script>fetch('https://api.example.com/data')</script>"
            "</body></html>"
        )

        fetcher = _html_fetcher(html)
        kwargs = _run_kwargs(fetcher)
        # Discovery omitted -> js_endpoints empty, no discovery diagnostics block
        r1 = await _run(policy=http_policy(discovery=None), **kwargs)
        assert r1.diagnostics["js_endpoints"] == []
        assert "discovery" not in r1.diagnostics
        # Content-only crawl: link extraction skipped entirely
        assert r1.discovered_link_count == 0
        # Discovery requested -> js_endpoints extracted; the (remote) sitemap/feed discovery is performed, so
        # the robots diagnostics block is populated (proving the gated function ran for this crawl).
        r2 = await _run(
            policy=http_policy(discovery={"robots": True, "sitemaps": True, "feeds": True}), **kwargs
        )
        assert isinstance(r2.diagnostics["js_endpoints"], list)
        assert r2.diagnostics["js_endpoints"]  # non-empty: HTML contains a fetch() endpoint
        assert "robots" in r2.diagnostics


async def test_recursive_crawl_drops_sitewide_lists_from_page_diagnostics(monkeypatch):
    # During recursive crawls, site-wide sitemap/feed URL lists are frontier seeds, not per-page payload --
    # returning them on every page's diagnostics wastes memory across thousands of pages.
    import app.transport.fetch_policy as http_mod

    async def fake_diag(**kwargs):
        return {
            "discovery": {"sitemap_urls": ["https://x.test/sitemap.xml"], "feeds": ["https://x.test/feed"]}
        }

    monkeypatch.setattr(http_mod, "discovery_diagnostics", fake_diag)

    fetcher = scripted_fetcher(status=200, ctype="text/html", body=b"<html><body>x</body></html>")

    kwargs = _run_kwargs(fetcher)
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
        from app.transport.cache import HttpCache

        # The cffi retry is the second step, so the sequence is the fixture: attempt one fails, attempt two
        # succeeds.
        retry_fetcher = sequenced_fetcher(
            [
                ConnectionError("first attempt fails"),
                (200, b"<html><body>recovered</body></html>", "text/html"),
            ],
            repeat_last=False,
        )

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

    The binary copy is rebuilt via ``asdict``, recursing the attempt list into
    plain dicts -- correct-looking, failing only when the adapter reads a field.
    """
    # An archive: inside the detector's real contract (PK magic under a non-text content type).
    archive = b"PK\x03\x04" + b"\x00" * 64

    binary_fetcher = scripted_fetcher(status=200, ctype="application/octet-stream", body=archive)

    result = await _run(**_run_kwargs(binary_fetcher))

    # The binary no-text terminal result: not an error, and not a render.
    assert result.classification == "binary_no_text"
    assert result.attempted_strategies
    # The declared type, not a dict that merely happens to have the right keys.
    assert all(isinstance(a, CrawlStrategyAttempt) for a in result.attempted_strategies)
    assert result.attempted_strategies[0].elapsed_ms > 0

    # And the adapter that reads those attributes must not raise.
    adapted = crawl_result_from_engine_result(result, url="http://x.test/")
    assert adapted.classification == "binary_no_text"
    assert adapted.elapsed_ms == result.attempted_strategies[0].elapsed_ms
    assert adapted.attempts == len(result.attempted_strategies)
