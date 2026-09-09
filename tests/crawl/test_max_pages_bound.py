from _testkit import site_fetcher

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from app.crawl.payload_helpers import _payload_policy
from app.settings import CrawlerSettings


async def test_max_pages_default_is_bounded_not_unlimited():
    settings = CrawlerSettings()
    policy = _payload_policy({"url": "http://example.com/", "policy": {}}, settings)
    # DEFAULT_MAX_PAGES, not 0/"unlimited" (F4).
    assert policy["limits"]["max_pages"] == 100


async def test_max_pages_zero_falls_back_to_default():
    settings = CrawlerSettings()
    policy = _payload_policy({"url": "http://example.com/", "policy": {"limits": {"max_pages": 0}}}, settings)
    assert policy["limits"]["max_pages"] == 100


async def test_max_pages_clamped_to_upper_bound():
    settings = CrawlerSettings()
    policy = _payload_policy(
        {"url": "http://example.com/", "policy": {"limits": {"max_pages": 99999}}}, settings
    )
    assert policy["limits"]["max_pages"] == 2000


async def test_max_pages_positive_preserved():
    settings = CrawlerSettings()
    policy = _payload_policy({"url": "http://example.com/", "policy": {"limits": {"max_pages": 7}}}, settings)
    assert policy["limits"]["max_pages"] == 7


async def test_max_pages_zero_bounded_at_engine():
    # Wide fixture: root links to 150 leaf pages. With max_pages=0 the crawl
    # must be bounded (DEFAULT_MAX_PAGES=100), not crawl all 151 reachable pages.
    pages = {
        "/": (200, "<html>" + "".join(f'<a href="/c{i}">c</a>' for i in range(150)) + "</html>", "text/html"),
    }
    for i in range(150):
        pages[f"/c{i}"] = (200, f"<html>child {i}</html>", "text/html")
    payload = {"url": "http://example.com/", "policy": {"limits": {"max_depth": 1, "max_pages": 0}}}
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    results = out["results"]
    assert 1 < len(results) <= 100
    # Without the bound it would have crawled root + 150 children = 151.
    assert len(results) < 151
