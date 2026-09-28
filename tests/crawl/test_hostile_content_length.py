"""L2: a hostile ``Content-Length`` must not crash the fetcher.

Both fetchers pre-check the declared length against the per-page cap. The
parse must treat a non-numeric (or negative, or absent) ``Content-Length`` as
"unknown size" and skip the pre-flight rejection -- the streamed running cap
still bounds the body -- rather than letting ``int("abc")`` raise out of the
fetch and turn one crafted response into that URL's failure.
"""

from app.crawl.strategies.fetchers import _default_http_fetcher
from app.crawl.strategies.pinned_transport import _declared_length_exceeds
from app.validation import HostPolicy
from tests._transport_doubles import MockResponse, patch_http_fetcher_transport


async def _fetch_behind_declared_length(monkeypatch, declared: str):
    """Serve one small HTML body behind a declared ``Content-Length`` and fetch it.

    The unknown-size case and the well-formed-oversized case below are the same
    fetch with a different header value: same transport, same page cap, same
    policy. Making the header the argument is what keeps the two from drifting
    into testing two different things, and it deletes the one-call
    ``_patch_transport`` indirection that only existed to be written twice.
    """
    patch_http_fetcher_transport(
        monkeypatch,
        [MockResponse(200, {"content-type": "text/html", "content-length": declared}, b"<html>ok</html>")],
        allow_ssrf_bypass=True,
    )
    return await _default_http_fetcher(
        "http://example.com/",
        {},
        30,
        1000,
        policy=HostPolicy.unrestricted(),
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
    result = await _fetch_behind_declared_length(monkeypatch, "abc")
    assert result.status_code == 200
    assert result.body == b"<html>ok</html>"


async def test_httpx_fetcher_still_rejects_valid_oversized_declared_length(monkeypatch):
    """A well-formed oversized Content-Length keeps the pre-flight 413 behavior."""
    result = await _fetch_behind_declared_length(monkeypatch, "5000")
    assert result.status_code == 413
