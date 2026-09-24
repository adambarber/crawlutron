"""Cookie bootstrap: after browser renders a blocked domain, retry cffi with
the browser-minted session cookies before accepting the render result."""

from _fetcher_factories import make_fetch

from app.crawl.strategy_chain import execute_crawl_chain
from app.models import CrawlResult
from app.validation import HostPolicy


def _crawl_result(url):
    return CrawlResult(
        url=url,
        status="success",
        classification="rendered",
        final_url=url,
        html="<html>rendered</html>",
        text="rendered",
        attempts=1,
        elapsed_ms=5,
        domain="example.com",
        metadata={},
    )


async def test_browser_cookies_replayed_through_cffi(monkeypatch):
    from app.crawl.strategies import browser as bmod

    monkeypatch.setitem(bmod._BROWSER_COOKIES_BY_DOMAIN, "example.com", "incap_ses=abc; visid=def")

    calls = []

    async def http_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        calls.append(("http", dict(headers)))
        return make_fetch(url=url, final_url=url, status=403, headers={}, body=b"denied")

    async def cffi_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        calls.append(("cffi", dict(headers)))
        if "incap_ses" in headers.get("Cookie", ""):
            return make_fetch(
                url=url,
                final_url=url,
                status=200,
                ctype="text/html",
                body=b"<html><body>cffi success</body></html>",
            )
        return make_fetch(url=url, final_url=url, status=403, headers={}, body=b"denied")

    async def browser_worker(job):
        return _crawl_result(job.url)

    policy = {
        "strategies": ["HTTP_DECLARED_CRAWLER", "HTTP_CFFI_IMPERSONATE", "BROWSER_HEADLESS"],
        "limits": {
            "timeout_seconds": 10,
            "max_retries": 0,
            "max_discovered_links": 10,
            "respect_robots": False,
        },
        "headers": {},
        "discovery": {"robots": True},
    }
    result = await execute_crawl_chain(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        policy=policy,
        http_fetcher=http_fetcher,
        cffi_fetcher=cffi_fetcher,
        browser_worker=browser_worker,
        host_policy=HostPolicy.unrestricted(),
    )
    assert result.status == "success"
    assert result.diagnostics["robots"]["evaluation"] == "disabled"
    # final cffi attempt must carry the browser cookies
    cffi_calls = [h for kind, h in calls if kind == "cffi"]
    assert any("incap_ses=abc" in h.get("Cookie", "") for h in cffi_calls), calls
