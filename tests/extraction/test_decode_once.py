"""#348: ``extract_content`` must decode an HTML body exactly once.

``is_html`` decodes bytes bodies for the sniff; the HTML branch used to decode
the same body a second time, so every bytes HTML page paid two full decodes.
"""

from __future__ import annotations

import pytest

from crawlutron.extraction import content as extraction

_HTML_CASES = [
    (
        "declared octet-stream, sniffed",
        b"<html><body><p>hello crawl</p></body></html>",
        "application/octet-stream",
    ),
    ("no content-type, sniffed", b"<!doctype html><html><body><p>sniffed</p></body></html>", None),
    ("declared text/html", b"<html><body><p>declared</p></body></html>", "text/html"),
]


@pytest.mark.parametrize(
    ("body", "content_type"), [(c[1], c[2]) for c in _HTML_CASES], ids=[c[0] for c in _HTML_CASES]
)
def test_html_body_decoded_once(monkeypatch, body, content_type):
    """A bytes body is decoded once: the sniff and the HTML branch share the text."""
    calls: list[bytes | bytearray | str] = []
    real_decode = extraction.decode_body

    def spy_decode(decoded_body):
        calls.append(decoded_body)
        return real_decode(decoded_body)

    monkeypatch.setattr(extraction, "decode_body", spy_decode)
    result = extraction.extract_content(body, content_type, None)
    assert result.text is not None
    assert len(calls) == 1, f"expected 1 decode, got {len(calls)}"


def test_str_body_never_decoded(monkeypatch):
    """A str body is already text: zero decode calls, same single dispatch."""
    calls: list[bytes | bytearray | str] = []

    def refusing_decode(body):
        calls.append(body)
        return extraction.decode_body(body)

    monkeypatch.setattr(extraction, "decode_body", refusing_decode)
    result = extraction.extract_content("<html><body><p>str</p></body></html>", "text/html", None)
    assert result.text is not None and "str" in result.text
    assert calls == [], "a str body must not pass through decode_body at all"
