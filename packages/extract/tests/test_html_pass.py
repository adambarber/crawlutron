"""HTML single pass: text, markdown, metadata, links, frames, forms, tables.

Adversarial matrix: deep nesting, unclosed tags, script-with-</p>, malformed
entities, duplicate attributes, null bytes, 5 MB single line, 100k-row table.
"""

from __future__ import annotations

from crawlutron_extract import ExtractLimits, Quality, extract


def test_basic_html_text_and_markdown():
    html = b"<h1>Title</h1><p>Hello <b>world</b></p>"
    result = extract(html, "text/html")
    assert result.quality == Quality.MEDIUM
    assert "Title" in result.text
    assert "Hello world" in result.text
    assert result.metadata["content_type"] == "text/html"


def test_headings_become_markdown():
    html = b"<h1>H1</h1><h3>H3</h3>"
    result = extract(html, "text/html")
    assert "# H1" in result.markdown
    assert "### H3" in result.markdown


def test_lists_become_markdown():
    html = b"<ul><li>a</li><li>b</li></ul><ol><li>x</li></ol>"
    result = extract(html, "text/html")
    assert "- a" in result.markdown
    assert "- b" in result.markdown
    assert "1. x" in result.markdown


def test_links_extracted():
    html = b'<a href="https://example.com">Example</a>'
    result = extract(html, "text/html")
    assert len(result.links) == 1
    assert result.links[0]["url"] == "https://example.com"
    assert result.links[0]["text"] == "Example"


def test_frames_extracted():
    html = b'<iframe src="https://example.com/frame" name="f1"></iframe>'
    result = extract(html, "text/html")
    assert len(result.frames) == 1
    assert result.frames[0]["src"] == "https://example.com/frame"


def test_forms_extracted():
    html = b'<form action="/submit" method="post"><input name="q" value="v"></form>'
    result = extract(html, "text/html")
    assert len(result.forms) == 1
    assert result.forms[0]["action"] == "/submit"
    assert result.forms[0]["method"] == "POST"
    assert len(result.forms[0]["fields"]) == 1


def test_tables_extracted():
    html = b"<table><tr><td>A</td><td>B</td></tr></table>"
    result = extract(html, "text/html")
    assert len(result.tables) == 1
    assert result.tables[0][0] == ["A", "B"]


def test_metadata_extracted():
    html = b'<title>Page Title</title><meta name="description" content="A page">'
    result = extract(html, "text/html")
    assert result.metadata.get("title") == "Page Title"
    assert result.metadata.get("description") == "A page"


def test_boilerplate_removed():
    html = b"<nav>Nav</nav><aside>Sidebar</aside><main>Main content</main>"
    result = extract(html, "text/html")
    assert "Nav" not in result.text
    assert "Sidebar" not in result.text
    assert "Main content" in result.text


def test_script_and_style_removed():
    html = b"<script>alert('x')</script><style>body{}</style><p>Visible</p>"
    result = extract(html, "text/html")
    assert "alert" not in result.text
    assert "body{}" not in result.text
    assert "Visible" in result.text


def test_role_boilerplate_removed():
    html = b'<div role="banner">Banner</div><div role="navigation">Nav</div><p>OK</p>'
    result = extract(html, "text/html")
    assert "Banner" not in result.text
    assert "Nav" not in result.text
    assert "OK" in result.text


def test_entity_decoded_once():
    html = b"<p>AT&amp;T</p>"
    result = extract(html, "text/html")
    assert "AT&T" in result.text
    assert "&amp;" not in result.text


def test_whitespace_collapsed():
    html = b"<p>  Hello   world  </p>"
    result = extract(html, "text/html")
    assert "Hello world" in result.text


def test_null_bytes_stripped():
    html = b"<p>Hel\x00lo</p>"
    result = extract(html, "text/html")
    assert "Hello" in result.text
    assert "\x00" not in result.text


def test_script_containing_close_p():
    html = b"<script>var x = '</p>';</script><p>After script</p>"
    result = extract(html, "text/html")
    assert "After script" in result.text


def test_malformed_entity_does_not_raise():
    html = b"<p>&notanentity;</p>"
    result = extract(html, "text/html")
    assert isinstance(result.text, str)


def test_duplicate_attributes_does_not_raise():
    html = b'<a href="one" href="two">Link</a>'
    result = extract(html, "text/html")
    assert isinstance(result.text, str)


def test_deep_nesting_does_not_raise():
    html = b"<div>" * 10000 + b"deep" + b"</div>" * 10000
    result = extract(html, "text/html")
    assert "deep" in result.text


def test_unclosed_tags_does_not_raise():
    html = b"<p>Unclosed<p>Still open"
    result = extract(html, "text/html")
    assert "Unclosed" in result.text


def test_5mb_single_line():
    html = b"<p>" + b"A" * 5_000_000 + b"</p>"
    limits = ExtractLimits(max_text_chars=100)
    result = extract(html, "text/html", limits=limits)
    assert len(result.text) <= 100
    assert result.truncated


def test_100k_row_table_capped():
    rows = b"".join(b"<tr><td>c1</td><td>c2</td></tr>" for _ in range(100_000))
    html = b"<table>" + rows + b"</table>"
    limits = ExtractLimits(max_table_rows=10)
    result = extract(html, "text/html", limits=limits)
    assert len(result.tables[0]) <= 10
    assert result.truncated


def test_links_capped():
    links = b"".join(b'<a href="u%d">t</a>' % i for i in range(2000))
    html = b"<div>" + links + b"</div>"
    limits = ExtractLimits(max_links=5)
    result = extract(html, "text/html", limits=limits)
    assert len(result.links) <= 5
    assert result.truncated


def test_main_content_preferred():
    html = b"<nav>Nav</nav><article>Article body</article><footer>Footer</footer>"
    result = extract(html, "text/html")
    assert "Article body" in result.text
    assert "Nav" not in result.text
    assert "Footer" not in result.text


def test_non_html_returns_low_quality():
    result = extract(b"hello", "text/plain")
    assert result.quality == Quality.LOW


def test_empty_html():
    result = extract(b"", "text/html")
    assert result.quality == Quality.LOW
    assert result.text == ""


def test_determinism():
    html = b"<h1>Test</h1><p>Hello world</p><a href='/x'>link</a>"
    limits = ExtractLimits()
    a = extract(html, "text/html", limits=limits)
    b = extract(html, "text/html", limits=limits)
    assert a == b
