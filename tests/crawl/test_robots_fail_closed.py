"""M7: robots.txt must fail closed, and group selection must use one token.

- A 5xx/error fetching robots.txt must disallow (fail closed, cached
  briefly), not yield empty rules that allow everything. 404 still means
  "no robots" and allows.
- _parse_robots_rules matched against the full User-Agent string while
  _parse_robots_crawl_delay matched the product token, and the rules
  docstring itself promises token matching. One token everywhere.
"""

from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl.engine import crawl_request
from app.models import HttpFetchResult
from app.settings import CrawlerSettings


def _make_fetcher(robots_status: int, robots_body: str = ""):
    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        if url.endswith("/robots.txt"):
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=robots_status,
                headers={"content-type": "text/plain"},
                body=robots_body.encode("utf-8"),
                elapsed_ms=1,
            )
        return HttpFetchResult(
            url=url,
            final_url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            body=b"<html><body>hi</body></html>",
            elapsed_ms=1,
        )

    return fetcher


def _policy():
    return {
        "strategies": [HTTP_DECLARED_CRAWLER],
        "limits": {
            "timeout_seconds": 10,
            "max_retries": 0,
            "max_discovered_links": 10,
            "respect_robots": True,
        },
    }


async def _crawl_page(host: str, fetcher):
    settings = CrawlerSettings.from_mapping()
    result = await crawl_request(
        {"url": f"http://{host}/page", "policy": _policy()},
        settings=settings,
        http_fetcher=fetcher,
    )
    return result["results"][0]


class TestRobotsFailClosed:
    async def test_robots_5xx_disallows(self):
        item = await _crawl_page("robots-fail.test", _make_fetcher(500))
        assert item["classification"] == "robots_disallowed"
        assert item["status"] != "success"

    async def test_robots_404_allows(self):
        item = await _crawl_page("robots-absent.test", _make_fetcher(404))
        assert item["status"] == "success"

    async def test_group_selection_uses_product_token(self):
        # "github" appears in the full User-Agent URL but not in the product
        # token: the github group must not apply to us; the * group allows.
        body = "User-agent: github\nDisallow: /\nUser-agent: *\nAllow: /\n"
        item = await _crawl_page("robots-token.test", _make_fetcher(200, body))
        assert item["status"] == "success"
