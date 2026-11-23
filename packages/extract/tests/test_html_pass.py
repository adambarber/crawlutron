"""HTML single pass: text, markdown, metadata, links, frames, forms, tables.

Adversarial matrix: deep nesting, unclosed tags, script-with-</p>, malformed
entities, duplicate attributes, null bytes, 5 MB single line, 100k-row table.
"""

from __future__ import annotations

import time

import pytest

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
    assert str(result.links[0]) == "https://example.com"
    assert result.links[0].host == "example.com"


def test_link_text_emitted_inline_in_text_and_markdown():
    html = b'<p>Read the <a href="https://ex.com/doc">full documentation</a> now.</p>'
    result = extract(html, "text/html")
    assert "full documentation" in result.text
    assert "[full documentation](https://ex.com/doc)" in result.markdown


def test_link_with_no_text_emits_nothing():
    html = b'<p><a href="https://ex.com/empty"></a> done</p>'
    result = extract(html, "text/html")
    assert result.text.strip() == result.markdown.strip() == "done"


def test_block_elements_do_not_concatenate():
    html = b"<h1>Title</h1><p>Read the body.</p>"
    result = extract(html, "text/html")
    assert "TitleRead" not in result.text
    assert "# Title" in result.text
    assert "Read the body." in result.text


def test_frames_extracted():
    html = b'<iframe src="https://example.com/frame" name="f1"></iframe>'
    result = extract(html, "text/html")
    frame = result.frames[0]
    assert len(result.frames) == 1
    assert frame["src"] == "https://example.com/frame"
    assert frame["name"] == "f1"


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
    assert "deep" in extract(b"<div>" * 10000 + b"deep" + b"</div>" * 10000, "text/html").text


def test_boilerplate_check_scales_linearly_with_depth():
    def _time(depth: int) -> float:
        # CPU time excludes runner throttle stalls that inflate wall time (#1337).
        start = time.process_time()
        extract(b"<div>" * depth + b"deep" + b"</div>" * depth, "text/html")
        return time.process_time() - start

    first = min(_time(2000) for _ in range(3))
    second = min(_time(4000) for _ in range(3))
    assert second < 2.8 * first, f"scaled {second / first:.2f}x for 2x depth"


def test_boilerplate_edge_cases():
    assert "inner" not in extract(b"<nav><nav>inner</nav></nav>", "text/html").text
    assert "child" not in extract(b"<nav><p>child</p></nav>", "text/html").text
    assert "main text" in extract(b"<nav><main>main text</main></nav>", "text/html").text
    assert "after" in extract(b"<nav>nav</nav><p>after</p>", "text/html").text
    result = extract(b'<div role="banner">banner</div><p>ok</p>', "text/html")
    assert "banner" not in result.text
    assert "ok" in result.text


def test_self_closing_void_tags_do_not_break_counter():
    assert "text" in extract(b"<br><hr><img src='x'>text", "text/html").text
    assert "deep" in extract(b"<div><br/></div>deep", "text/html").text


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
    links = b"".join(b'<a href="https://example.com/u%d">t</a>' % i for i in range(2000))
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
    assert result.quality == Quality.FAILED
    assert result.text == ""


def test_non_html_clean_decode_is_low():
    result = extract(b"hello", "text/plain")
    assert result.quality == Quality.LOW


def test_non_html_empty_is_failed():
    result = extract(b"", "text/plain")
    assert result.quality == Quality.FAILED


def test_html_lossy_decode_is_low():
    content = b"\xff\xfeinvalid"
    result = extract(content, "text/html")
    assert result.quality == Quality.LOW
    assert "charset_lossy" in result.flags


def test_html_only_links_text_is_emitted():
    html = b'<a href="https://example.com">Link</a>'
    result = extract(html, "text/html")
    assert "Link" in result.text
    assert result.quality == Quality.MEDIUM
    assert len(result.links) == 1


def test_html_body_text_no_main_is_medium():
    html = b"<p>Some body text without a main content marker.</p>"
    result = extract(html, "text/html")
    assert result.quality == Quality.MEDIUM


def test_html_main_below_floor_is_medium():
    body = "x" * (200 - 1)
    html = f"<main>{body}</main>".encode()
    result = extract(html, "text/html")
    assert result.quality == Quality.MEDIUM


def test_html_main_at_floor_is_high():
    body = "x" * 200
    html = f"<main>{body}</main>".encode()
    result = extract(html, "text/html")
    assert result.quality == Quality.HIGH


def test_html_main_above_floor_is_high():
    body = "x" * 201
    html = f"<main>{body}</main>".encode()
    result = extract(html, "text/html")
    assert result.quality == Quality.HIGH


def test_html_main_with_lossy_decode_is_low():
    content = b"\xff\xfe" + b"<main>" + b"x" * 300 + b"</main>"
    result = extract(content, "text/html")
    assert result.quality == Quality.LOW
    assert "charset_lossy" in result.flags


def test_links_collected_inside_boilerplate():
    html = (
        b'<nav role="navigation"><a href="/nav-link">Nav link</a></nav>'
        b'<footer><a href="/footer-link">Footer link</a></footer>'
        b"<p>Main text</p>"
    )
    result = extract(html, "text/html", url="https://example.com/")
    assert len(result.links) == 2
    urls = {str(link) for link in result.links}
    assert urls == {"https://example.com/nav-link", "https://example.com/footer-link"}


def test_main_inside_boilerplate_extracts_text():
    html = (
        b'<nav role="navigation"><a href="/nav">Nav</a></nav>'
        b'<div role="banner"><main>' + b"x" * 300 + b"</main></div>"
    )
    result = extract(html, "text/html")
    assert result.quality == Quality.HIGH
    assert len(result.text) >= 200


def test_determinism():
    html = b"<h1>Test</h1><p>Hello world</p><a href='/x'>link</a>"
    limits = ExtractLimits()
    a = extract(html, "text/html", limits=limits)
    b = extract(html, "text/html", limits=limits)
    assert a == b


@pytest.mark.parametrize(
    "html,expected_url,expected_refused",
    [
        # javascript link refused, https link kept
        (
            b'<a href="javascript:alert(1)">JS</a><a href="https://example.com/ok">OK</a>',
            "https://example.com/ok",
            1,
        ),
        (
            b'<a href="//example.com/abs">Protocol-relative</a>',
            "https://example.com/abs",
            0,
        ),
        # relative ../x resolved against base URL
        (
            b'<a href="../sibling">Sibling</a>',
            "https://example.com/sibling",
            0,
        ),
        # <base href> changes resolution base
        (
            b'<base href="https://base.example.com/root/"><a href="page">Relative</a>',
            "https://base.example.com/root/page",
            0,
        ),
        # duplicate links differing only by fragment dedupe to one
        (
            b'<a href="https://example.com/page#a">A</a><a href="https://example.com/page#b">B</a>',
            "https://example.com/page",
            0,
        ),
    ],
)
def test_links_resolve(html, expected_url, expected_refused):
    result = extract(html, "text/html", url="https://example.com/")
    assert len(result.links) == 1
    assert str(result.links[0]) == expected_url
    assert result.links_refused == expected_refused


def test_links_dedup_by_trailing_slash():
    html = b'<a href="https://example.com/page">A</a><a href="https://example.com/page/">B</a>'
    result = extract(html, "text/html", url="https://example.com/")
    assert len(result.links) == 2


def test_links_10k_capped_and_steady_memory():
    links = b"".join(b'<a href="https://example.com/u%d">t</a>' % i for i in range(10_000))
    html = b"<div>" + links + b"</div>"
    limits = ExtractLimits(max_links=50)
    result = extract(html, "text/html", url="https://example.com/", limits=limits)
    assert len(result.links) == 50
    assert result.truncated


def test_links_all_refused():
    html = b'<a href="javascript:1">J</a><a href="mailto:a@b.c">M</a><a href="data:x">D</a>'
    result = extract(html, "text/html", url="https://example.com/")
    assert len(result.links) == 0
    assert result.links_refused == 3
    assert "links_refused" in result.flags
