"""Discovered links: base href resolution, scheme drops, dedup, cap, refusals."""

from __future__ import annotations

from crawlutron_extract import (
    ExtractLimits,
    LinkRecord,
    extract_discovered_links,
)


def _policy():
    from safe_url import HostPolicy

    return HostPolicy.unrestricted()


def test_base_href_changes_resolution():
    html = '<base href="http://other.com/root/"><a href="page">Page</a>'
    links, refused = extract_discovered_links(html, "http://example.com/")
    assert refused == 0
    assert [str(link.normalized_url) for link in links] == ["http://other.com/root/page"]


def test_fragment_javascript_mailto_data_dropped():
    html = (
        '<a href="#frag">frag</a>'
        '<a href="javascript:go()">js</a>'
        '<a href="mailto:x@y">mail</a>'
        '<a href="data:text,hi">data</a>'
        '<a href="http://example.com/ok">ok</a>'
    )
    links, refused = extract_discovered_links(html, "http://example.com/")
    assert refused == 4
    assert [str(link.normalized_url) for link in links] == ["http://example.com/ok"]


def test_duplicates_by_fragment_and_trailing_slash_collapse():
    html = (
        '<a href="http://example.com/one#a">One</a>'
        '<a href="http://example.com/one#b">One again</a>'
        '<a href="http://example.com/two/">Two</a>'
        '<a href="http://example.com/two">Two again</a>'
    )
    links, refused = extract_discovered_links(html, "http://example.com/")
    assert len(links) == 3
    assert refused == 0


def test_same_host_across_boundary():
    html = '<a href="http://example.com/same">Same</a><a href="http://other.com/other">Other</a>'
    links, refused = extract_discovered_links(html, "http://example.com/")
    assert refused == 0
    assert links[0].is_same_host is True
    assert links[1].is_same_host is False


def test_cap_after_admission_counts_refusals():
    links, refused = extract_discovered_links(
        (
            '<a href="http://example.com/a">A</a>'
            '<a href="http://example.com/b">B</a>'
            '<a href="http://example.com/c">C</a>'
            '<a href="javascript:x">refused</a>'
        ),
        "http://example.com/",
        limits=ExtractLimits(max_links=2),
    )
    assert len(links) == 2
    assert refused == 1


def test_all_refused_yields_empty_and_count():
    html = '<a href="mailto:x@y">mail</a><a href="javascript:z">js</a>'
    links, refused = extract_discovered_links(html, "http://example.com/")
    assert links == []
    assert refused == 2


def test_link_record_carries_text_rel_and_raw():
    html = '<a href="http://example.com/p" rel="nofollow next">text here</a>'
    links, _ = extract_discovered_links(html, "http://example.com/")
    rec = links[0]
    assert isinstance(rec, LinkRecord)
    assert rec.raw_href == "http://example.com/p"
    assert rec.text == "text here"
    assert rec.rel == ("nofollow", "next")
