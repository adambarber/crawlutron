"""#2 Site-map aggregation - pure tests (no mocks)."""

from app.map import build_site_map



def test_build_site_map_aggregates_and_dedupes():
    crawl_response = {
        "results": [{
            "original_url": "http://x.example/",
            "final_url": "http://x.example/",
            "diagnostics": {
                "robots": {"sitemaps": ["http://x.example/sitemap.xml", "http://x.example/sitemap2.xml"]},
                "discovery": {"sitemap_urls": ["http://x.example/sitemap.xml"], "feeds": ["http://x.example/feed.xml"]},
            },
            "discovered_links": [
                {"url": "http://x.example/a", "normalized_url": "http://x.example/a"},
                {"url": "http://x.example/b", "normalized_url": "http://x.example/b"},
                {"url": "http://x.example/a", "normalized_url": "http://x.example/a"},
            ],
        }]
    }
    m = build_site_map(crawl_response)
    assert m["url"] == "http://x.example/"
    assert m["links"] == ["http://x.example/a", "http://x.example/b"]
    assert "http://x.example/sitemap.xml" in m["sitemaps"]
    assert "http://x.example/sitemap2.xml" in m["sitemaps"]
    assert m["feeds"] == ["http://x.example/feed.xml"]
    assert m["all_urls"] == [
        "http://x.example/a", "http://x.example/b",
        "http://x.example/sitemap.xml", "http://x.example/sitemap2.xml",
        "http://x.example/feed.xml",
    ]


def test_build_site_map_handles_dataclass_links():
    from app.models import DiscoveredLink
    link = DiscoveredLink(
        url="u", normalized_url="n", text="", rel=[], source_url="s", host="h", is_same_host=True
    )
    crawl_response = {"results": [{"final_url": "http://r/", "discovered_links": [link], "diagnostics": {}}]}
    m = build_site_map(crawl_response)
    assert m["links"] == ["n"]
    assert m["all_urls"] == ["n"]


def test_build_site_map_empty():
    m = build_site_map({"results": []})
    assert m["url"] is None
    assert m["all_urls"] == []
