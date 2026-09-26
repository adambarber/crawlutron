"""L2: a hostile ``Content-Length`` must not crash the fetcher.

Both fetchers pre-check the declared length against the per-page cap. The
parse must treat a non-numeric (or negative, or absent) ``Content-Length`` as
"unknown size" and skip the pre-flight rejection -- the streamed running cap
still bounds the body -- rather than letting ``int("abc")`` raise out of the
fetch and turn one crafted response into that URL's failure.
"""

from _transport_doubles import MockResponse, patch_http_fetcher_transport

from app.crawl.strategies.fetchers import _default_http_fetcher
from app.crawl.strategies.pinned_transport import _declared_length_exceeds
from app.validation import HostPolicy


def _patch_transport(monkeypatch, response):
    return patch_http_fetcher_transport(
        monkeypatch,
        [response],
        allow_ssrf_bypass=True,
    )


class TestDeclaredLengthPredicate:
    def test_numeric_over_limit_is_oversized(self):
        assert _declared_length_exceeds("2000", 1000) is True

    def test_numeric_under_limit_is_allowed(self):
        assert _declared_length_exceeds("500", 1000) is False

    def test_garbage_is_treated_as_unknown_not_a_crash(self):
        # The bug: int("abc") raised. A hostile header means "no declared size".
        assert _declared_length_exceeds("abc", 1000) is False
        assert _declared_length_exceeds("12x", 1000) is False

    def test_negative_is_treated_as_unknown(self):
        assert _declared_length_exceeds("-5", 1000) is False

    def test_absent_is_treated_as_unknown(self):
        assert _declared_length_exceeds(None, 1000) is False
        assert _declared_length_exceeds("", 1000) is False

    def test_float_shaped_value_is_treated_as_unknown(self):
        assert _declared_length_exceeds("12.5", 1000) is False


async def test_httpx_fetcher_serves_body_despite_garbage_content_length(monkeypatch):
    """A small body under a bogus Content-Length streams normally (no raise)."""
    _patch_transport(
        monkeypatch,
        MockResponse(200, {"content-type": "text/html", "content-length": "abc"}, b"<html>ok</html>"),
    )
    result = await _default_http_fetcher(
        "http://example.com/",
        {},
        30,
        1000,
        policy=HostPolicy.unrestricted(),
    )
    assert result.status_code == 200
    assert result.body == b"<html>ok</html>"


async def test_httpx_fetcher_still_rejects_valid_oversized_declared_length(monkeypatch):
    """A well-formed oversized Content-Length keeps the pre-flight 413 behavior."""
    _patch_transport(
        monkeypatch,
        MockResponse(200, {"content-type": "text/html", "content-length": "5000"}, b"<html>ok</html>"),
    )
    result = await _default_http_fetcher(
        "http://example.com/",
        {},
        30,
        1000,
        policy=HostPolicy.unrestricted(),
    )
    assert result.status_code == 413
