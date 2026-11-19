"""Sitemap locs: urlset locs and sitemapindex child sitemaps as their own list."""

from __future__ import annotations

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
