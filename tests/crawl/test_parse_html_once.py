"""#11: parse each HTML page once and reuse the lxml tree across passes.

Previously ``_html_diagnostics`` reparsed for frames and forms (2 parses) and
``_extract_discovered_links`` parsed again (1 more) -- 3 lxml parses per page
for the same bytes. These tests assert the parse happens once when a tree is
shared, and that ``_html_diagnostics`` itself only parses a single tree.
"""

import app.crawl.extraction as extraction


def _spy_parse(monkeypatch):
    calls = {"n": 0}
    real = extraction._parse_html_tree

    def spy(html):
        calls["n"] += 1
        return real(html)

    monkeypatch.setattr(extraction, "_parse_html_tree", spy)
    return calls


def test_html_diagnostics_parses_once(monkeypatch):
    calls = _spy_parse(monkeypatch)
    html = (
        "<html><body><a href='/x'>x</a>"
        "<form action='/s'><input name='q'></form></body></html>"
    )
    extraction._html_diagnostics(html, "http://example.com/")
    assert calls["n"] == 1


def test_shared_tree_parses_once_across_passes(monkeypatch):
    calls = _spy_parse(monkeypatch)
    html = (
        "<html><body><a href='/x'>x</a><iframe src='/f'></iframe>"
        "<form action='/s'><input name='q'></form></body></html>"
    )
    tree = extraction._parse_html_tree(html)
    assert calls["n"] == 1
    extraction._html_diagnostics(html, "http://example.com/", tree=tree)
    links, _, _ = extraction._extract_discovered_links(
        html, "http://example.com/", tree=tree
    )
    # frames, forms, and links all reused the single pre-parsed tree.
    assert calls["n"] == 1
    assert links
