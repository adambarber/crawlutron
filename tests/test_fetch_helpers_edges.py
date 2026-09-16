"""Fetch helper edge cases: sitemap recursion, rate limiting, robots fetching."""

from coverage_helpers import *

from app.constants import status_for_classification


class TestFetchHelpersEdges:
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
        assert status_for_classification("not_modified") == "success"

    def test_quality_frameset_low_text(self):
        score = _quality_score("success", "rendered", "tiny", "<frameset></frameset>", 200)
        assert score <= 0.4

    @pytest.mark.parametrize("text,html", [(None, None), ("", None), ("hi", "<p>x</p>"), ("a" * 30, "<html><body>x</body></html>")])
    def test_quality_score_reuses_precomputed_metrics(self, text, html):
        """Dup8: threading the diagnostics metrics in must not change the score."""
        from app.crawl.strategies.http import _content_metrics

        metrics = _content_metrics(text, html)
        direct = _quality_score("success", "rendered", text, html, 200)
        threaded = _quality_score("success", "rendered", text, html, 200, content_metrics=metrics)
        assert threaded == direct

    def test_quality_score_ignores_metrics_when_not_rendered(self):
        """The early return must not read the metrics dict (None is never indexed)."""
        assert _quality_score("failed", "network_error", None, None, None, content_metrics=None) == 0.0

    def test_decode_body_gzip_content_encoding(self):
        # Decompression happens at the capped chokepoint, then decode.
        body = gzip.compress(b"hello world")
        out = _decode_body(decompress_body(body, "gzip", max_output_bytes=1_000_000), "text/html")
        assert out == "hello world"
