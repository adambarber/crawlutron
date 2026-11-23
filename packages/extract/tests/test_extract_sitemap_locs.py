"""Sitemap locs: urlset locs and sitemapindex child sitemaps as their own list."""

from __future__ import annotations

import time

from crawlutron_extract import ExtractLimits, extract_sitemap_locs


def test_flat_urlset():
    xml = (
        "<urlset><url><loc>http://example.com/a</loc></url>"
        "<url><loc>http://example.com/b</loc></url></urlset>"
    )
    locs, sitemaps = extract_sitemap_locs(xml)
    assert [str(loc) for loc in locs] == ["http://example.com/a", "http://example.com/b"]
    assert [str(sitemap) for sitemap in sitemaps] == []


def test_sitemapindex_children_are_their_own_list():
    xml = (
        "<sitemapindex><sitemap><loc>http://example.com/s2.xml</loc></sitemap>"
        "<sitemap><loc>http://example.com/s3.xml</loc></sitemap></sitemapindex>"
    )
    locs, sitemaps = extract_sitemap_locs(xml)
    assert [str(loc) for loc in locs] == []
    assert [str(sitemap) for sitemap in sitemaps] == [
        "http://example.com/s2.xml",
        "http://example.com/s3.xml",
    ]


def test_mixed_urlset_and_sitemapindex():
    xml = (
        "<sitemapindex><sitemap><loc>http://example.com/s2.xml</loc></sitemap></sitemapindex>"
        "<urlset><url><loc>http://example.com/a</loc></url></urlset>"
    )
    locs, sitemaps = extract_sitemap_locs(xml)
    assert [str(loc) for loc in locs] == ["http://example.com/a"]
    assert [str(sitemap) for sitemap in sitemaps] == ["http://example.com/s2.xml"]


def test_loc_whitespace_and_newlines_stripped():
    xml = "<urlset><url><loc>  http://example.com/a \n</loc></url></urlset>"
    locs, _ = extract_sitemap_locs(xml)
    assert [str(loc) for loc in locs] == ["http://example.com/a"]


def test_dtd_declared_document_refused():
    xml = '<!DOCTYPE urlset [<!ENTITY x "y">]><urlset><url><loc>http://example.com/a</loc></url></urlset>'
    assert extract_sitemap_locs(xml) == ([], [])


def test_entity_declared_document_refused():
    xml = '<?xml version="1.0"?><!ENTITY x "y"><urlset><url><loc>http://example.com/a</loc></url></urlset>'
    assert extract_sitemap_locs(xml) == ([], [])


def test_relative_loc_refused_without_base():
    assert extract_sitemap_locs("<urlset><url><loc>/a</loc></url></urlset>") == ([], [])


def test_javascript_loc_refused():
    assert extract_sitemap_locs("<urlset><url><loc>javascript:alert(1)</loc></url></urlset>") == ([], [])


def test_deduped_and_capped():
    xml = (
        "<urlset>" + "".join(f"<url><loc>http://example.com/{i}</loc></url>" for i in range(5)) + "</urlset>"
    )
    locs, _ = extract_sitemap_locs(xml, limits=ExtractLimits(max_links=2))
    assert len(locs) == 2


def test_empty_xml_returns_empty():
    assert extract_sitemap_locs("") == ([], [])


def test_loc_inside_sitemap_goes_to_sitemap_locs():
    xml = (
        "<sitemapindex><sitemap><loc>http://example.com/s2.xml</loc></sitemap>"
        "<urlset><url><loc>http://example.com/a</loc></url></urlset>"
    )
    locs, sitemaps = extract_sitemap_locs(xml)
    assert [str(loc) for loc in locs] == ["http://example.com/a"]
    assert [str(sitemap) for sitemap in sitemaps] == ["http://example.com/s2.xml"]


def test_loc_after_sitemap_close_goes_to_page_locs():
    xml = (
        "<sitemap><loc>http://example.com/s2.xml</loc></sitemap>"
        "<urlset><url><loc>http://example.com/a</loc></url></urlset>"
    )
    locs, sitemaps = extract_sitemap_locs(xml)
    assert [str(loc) for loc in locs] == ["http://example.com/a"]
    assert [str(sitemap) for sitemap in sitemaps] == ["http://example.com/s2.xml"]


def test_deeply_nested_sitemap_tracks_depth():
    xml = (
        "<sitemap><sitemap><loc>http://example.com/deep.xml</loc></sitemap>"
        "<loc>http://example.com/mid.xml</loc></sitemap>"
        "<urlset><url><loc>http://example.com/a</loc></url></urlset>"
    )
    locs, sitemaps = extract_sitemap_locs(xml)
    assert [str(loc) for loc in locs] == ["http://example.com/a"]
    assert [str(sitemap) for sitemap in sitemaps] == [
        "http://example.com/deep.xml",
        "http://example.com/mid.xml",
    ]


def test_max_input_bytes_refuses_oversized_input():
    limits = ExtractLimits(max_input_bytes=10)
    xml = b"<urlset><url><loc>http://example.com/a</loc></url></urlset>"
    assert extract_sitemap_locs(xml, limits=limits) == ([], [])


def test_input_within_limit_still_parsed():
    limits = ExtractLimits(max_input_bytes=10_000)
    xml = "<urlset><url><loc>http://example.com/a</loc></url></urlset>"
    locs, _ = extract_sitemap_locs(xml, limits=limits)
    assert [str(loc) for loc in locs] == ["http://example.com/a"]


def test_large_input_completes_and_is_correct():
    n = 50_000
    limits = ExtractLimits(max_links=n)
    parts = ["<urlset>"]
    for i in range(n):
        parts.append(f"<url><loc>http://example.com/{i}</loc></url>")
    parts.append("</urlset>")
    locs, _ = extract_sitemap_locs("".join(parts), limits=limits)
    assert len(locs) == n
    assert [str(loc) for loc in locs[:3]] == [
        "http://example.com/0",
        "http://example.com/1",
        "http://example.com/2",
    ]


def test_doubling_loc_count_grows_linearly_not_quadratically():
    def build(count: int) -> str:
        parts = ["<sitemapindex>"]
        for i in range(count):
            parts.append(f"<sitemap><loc>http://example.com/s{i}.xml</loc></sitemap>")
        parts.append("</sitemapindex>")
        return "".join(parts)

    small_xml = build(3000)
    limits = ExtractLimits(max_links=6000)
    t0 = time.perf_counter()
    _, s_small = extract_sitemap_locs(small_xml, limits=limits)
    t1 = time.perf_counter()
    large_xml = build(6000)
    t2 = time.perf_counter()
    _, s_large = extract_sitemap_locs(large_xml, limits=limits)
    t3 = time.perf_counter()
    assert len(s_small) == 3000
    assert len(s_large) == 6000
    small_dt = t1 - t0
    large_dt = t3 - t2
    # Linear growth: 2x input must be well under 4x time (quadratic ~4x).
    assert large_dt < 4.0 * small_dt + 1.0, f"not linear: {small_dt:.3f}s -> {large_dt:.3f}s"
