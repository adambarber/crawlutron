"""Characterization test for the recursive crawl frontier (MEDIUM: deque).

The frontier was a list using O(n) pop(0); it is now a collections.deque with
O(1) popleft. Behavior (BFS completeness/ordering) must be unchanged.
"""

from _fetcher_factories import site_fetcher

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request


def _build_hub(n_leaves: int):
    leaves = " ".join(f'<a href="/l{i}">l{i}</a>' for i in range(n_leaves))
    pages = {
        "/": (200, "<html><body><a href='/hub'>hub</a></body></html>", "text/html"),
        "/hub": (200, f"<html><body>{leaves}</body></html>", "text/html"),
    }
    for i in range(n_leaves):
        pages[f"/l{i}"] = (200, f"<html><body>leaf {i}</body></html>", "text/html")
    return pages


async def test_bfs_crawls_all_discovered_leaves():
    pages = _build_hub(300)
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": 2, "max_pages": 1000, "max_discovered_links": 2000}},
    }
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    urls = {r["normalized_url"] for r in out["results"]}
    # root, hub, and all 300 leaves must be present.
    assert "http://example.com" in urls
    assert "http://example.com/hub" in urls
    for i in range(300):
        assert f"http://example.com/l{i}" in urls


async def test_frontier_processes_in_bfs_order():
    # A 2-level graph: root -> a,b ; a -> a1,a2 ; b -> b1,b2.
    pages = {
        "/": (200, '<html><body><a href="/a">a</a><a href="/b">b</a></body></html>', "text/html"),
        "/a": (200, '<html><body><a href="/a1">a1</a><a href="/a2">a2</a></body></html>', "text/html"),
        "/b": (200, '<html><body><a href="/b1">b1</a><a href="/b2">b2</a></body></html>', "text/html"),
        "/a1": (200, "<html></html>", "text/html"),
        "/a2": (200, "<html></html>", "text/html"),
        "/b1": (200, "<html></html>", "text/html"),
        "/b2": (200, "<html></html>", "text/html"),
    }
    payload = {"url": "http://example.com/", "policy": {"limits": {"max_depth": 2, "max_pages": 20}}}
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    urls = [r["normalized_url"] for r in out["results"]]
    # BFS: root, then a and b (depth 1), then their children (depth 2).
    assert urls.index("http://example.com/a") < urls.index("http://example.com/a1")
    assert urls.index("http://example.com/a") < urls.index("http://example.com/a2")
    assert urls.index("http://example.com/b") < urls.index("http://example.com/b1")
