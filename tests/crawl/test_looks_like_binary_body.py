"""LOW fix regression: looks_like_binary_body magic-number check.

The historical bug used a duplicated b"PK" and a single-byte b"\xe3" (0xE3) that
false-positived on ordinary UTF-8 text (e.g. any 3-byte sequence led by 0xE3,
common in CJK). The correct magic bytes are PK\x03\x04 (zip local header),
PK\x05\x06 (zip EOCD), and \x1f\x8b (gzip). These tests lock that in.
"""

from app.transport.fetch_policy import looks_like_binary_body
from tests._policy_factories import canonical


def test_zip_local_header_detected():
    assert (
        looks_like_binary_body(b"PK\x03\x04data", "application/octet-stream", canonical("http://x/f")) is True
    )


def test_zip_eocd_detected():
    assert looks_like_binary_body(b"PK\x05\x06trailing", "application/zip", canonical("http://x/f")) is True


def test_gzip_magic_detected():
    assert (
        looks_like_binary_body(b"\x1f\x8b\x08", "application/octet-stream", canonical("http://x/f")) is True
    )


def test_utf8_text_with_c3_lead_not_flagged():
    # 'a with tilde' in UTF-8 is bytes 0xC3 0xA3; must not be treated as binary.
    body = "\u00e3bc".encode("utf-8")
    assert looks_like_binary_body(body, None, canonical("http://x/page")) is False


def test_utf8_text_with_e3_lead_not_flagged():
    # A 3-byte UTF-8 sequence led by 0xE3 (e.g. a CJK codepoint) must not be
    # misclassified as a binary magic number.
    assert looks_like_binary_body(b"\xe3\x80\x80rest", None, canonical("http://x/page")) is False


def test_plain_text_html_not_flagged():
    assert (
        looks_like_binary_body(b"<html><body>hi</body></html>", "text/html", canonical("http://x/")) is False
    )
