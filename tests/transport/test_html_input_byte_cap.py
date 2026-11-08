"""#655: the HTML input cap truncates by BYTES, not characters -- a multibyte-heavy
page must never exceed the documented byte budget after truncation."""

from __future__ import annotations

from crawlutron.safety.bounded_io import MAX_HTML_INPUT_BYTES, truncate_html_input


def test_ascii_html_under_the_cap_passes_through():
    html = "<html><body>plain</body></html>"
    assert truncate_html_input(html) == html


def test_none_becomes_empty():
    assert truncate_html_input(None) == ""


def test_multibyte_page_truncates_to_the_byte_cap():
    """#655: one character per three UTF-8 bytes means the old character cut
    returned ~3x the byte budget; the truncation must be byte-accurate."""
    html = "ę" * (MAX_HTML_INPUT_BYTES // 2 + 100)  # each 'ę' is 2 UTF-8 bytes
    assert len(html) < MAX_HTML_INPUT_BYTES, "the test page must be under the cap in CHARACTERS"
    assert len(html.encode("utf-8")) > MAX_HTML_INPUT_BYTES, "but over it in BYTES"
    truncated = truncate_html_input(html)
    assert len(truncated.encode("utf-8")) <= MAX_HTML_INPUT_BYTES
