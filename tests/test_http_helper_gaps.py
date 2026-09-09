"""TestHttpHelperGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *


class TestHttpHelperGaps:
    def test_cookie_header_per_host(self):
        assert (
            _cookie_header_for_url(
                {"cookie_jar": "per_host", "cookies": {"e.com": {"a": "1"}}},
                "http://e.com/",
            )
            == "a=1"
        )

    def test_headers_cffi_impersonate(self):
        headers = _headers_for_strategy(
            HTTP_CFFI_IMPERSONATE,
            {"request_profile": {}, "headers": {}},
            "http://e.com/",
        )
        assert "User-Agent" in headers
        # Firefox identity (matching the firefox135 impersonation) sends no
        # Sec-CH-UA client hints -- those are Chromium-only.
        assert "sec-ch-ua" not in {k.lower() for k in headers}
        assert "Firefox" in headers["User-Agent"]

    def test_headers_cookie_added(self):
        headers = _headers_for_strategy(
            HTTP_DECLARED_CRAWLER,
            {"request_profile": {"cookie_jar": "per_host", "cookies": {"e.com": {"a": "1"}}}, "headers": {}},
            "http://e.com/",
        )
        assert headers.get("Cookie") == "a=1"

    def test_status_for_not_modified(self):
        assert _status_for_classification("not_modified") == "success"

    def test_quality_frameset_low_text(self):
        score = _quality_score("success", "rendered", "tiny", "<frameset></frameset>", 200)
        assert score <= 0.4

    def test_decode_body_gzip_content_encoding(self):
        body = gzip.compress(b"hello world")
        out = _decode_body(body, "text/html", {"content-encoding": "gzip"})
        assert out == "hello world"
