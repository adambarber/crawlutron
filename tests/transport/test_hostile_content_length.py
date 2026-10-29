"""A hostile ``Content-Length`` must not crash the fetcher.

A non-numeric / negative / absent value must read as "unknown size" and skip
pre-flight rejection -- not raise ``int("abc")`` out of the fetch.
"""

from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.validation import HostPolicy
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.transport.pinned_transport import declared_length_exceeds
from tests._transport_doubles import MockResponse, patch_http_fetcher_transport


async def _fetch_behind_declared_length(monkeypatch, declared: str):
    """Serve one small HTML body behind a declared ``Content-Length`` and fetch it.

    The unknown-size and well-formed-oversized cases are the same fetch with a
    different header value -- the header-as-argument keeps the two from drifting.
    """
    patch_http_fetcher_transport(
        monkeypatch,
        [MockResponse(200, {"content-type": "text/html", "content-length": declared}, b"<html>ok</html>")],
        allow_ssrf_bypass=True,
    )
    return await default_http_fetcher(
        NormalizedUrl("http://example.com/", policy=HostPolicy.unrestricted()),
        {},
        30,
        1000,
    )


class TestDeclaredLengthPredicate:
    def test_numeric_over_limit_is_oversized(self):
        assert declared_length_exceeds("2000", 1000) is True

    def test_numeric_under_limit_is_allowed(self):
        assert declared_length_exceeds("500", 1000) is False

    def test_garbage_is_treated_as_unknown_not_a_crash(self):
        # The bug: int("abc") raised. A hostile header means "no declared size".
        assert declared_length_exceeds("abc", 1000) is False
        assert declared_length_exceeds("12x", 1000) is False

    def test_negative_is_treated_as_unknown(self):
        assert declared_length_exceeds("-5", 1000) is False

    def test_absent_is_treated_as_unknown(self):
        assert declared_length_exceeds(None, 1000) is False
        assert declared_length_exceeds("", 1000) is False

    def test_float_shaped_value_is_treated_as_unknown(self):
        assert declared_length_exceeds("12.5", 1000) is False


async def test_httpx_fetcher_serves_body_despite_garbage_content_length(monkeypatch):
    """A small body under a bogus Content-Length streams normally (no raise)."""
    result = await _fetch_behind_declared_length(monkeypatch, "abc")
    assert result.status_code == 200
    assert result.body == b"<html>ok</html>"


async def test_httpx_fetcher_still_rejects_valid_oversized_declared_length(monkeypatch):
    """A well-formed oversized Content-Length keeps the pre-flight 413 behavior."""
    result = await _fetch_behind_declared_length(monkeypatch, "5000")
    assert result.status_code == 413
