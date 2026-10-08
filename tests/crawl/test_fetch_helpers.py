"""Fetch helpers: the shared discovery fetch+decompress+decode core.

``fetch_and_decode`` (robots gating in the engine) and ``_fetch_text``
(sitemap fan-out) each used to hand-roll the same fetch -> await ->
lowercase headers -> capped decompress -> decode sequence. That core now
lives in one place (``_fetch_decode_discovery``); these tests pin both call
sites' contracts so the collapse is provably behavior-preserving.
"""

import gzip
from dataclasses import dataclass, field

from app.crawl.fetch_helpers import _fetch_decode_discovery, _fetch_text, fetch_and_decode
from app.vocabulary.limits import DISCOVERY_BODY_MAX_BYTES


class _FakeResp:
    def __init__(self, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body


def _gzip_bomb() -> bytes:
    """A gzipped body that decompresses to more than the discovery cap allows.

    One construction because "over the cap" is a fact about the decoder, not about
    either caller -- and a bomb sized against a constant that later moved was a
    bomb that stopped testing the cap without anything going red.
    """
    return gzip.compress(b"\x00" * (DISCOVERY_BODY_MAX_BYTES + 4096))


@dataclass
class _RecordingFetcher:
    """A fetcher that always returns ``resp`` and records its call arguments.

    A class rather than a closure with a bolted-on attribute: ``calls`` is part
    of what a test inspects, so it is declared here instead of attached at
    runtime and hidden from the type checker.
    """

    resp: object
    calls: list[tuple] = field(default_factory=list)

    def __call__(self, url, headers, timeout_seconds):
        self.calls.append((url, dict(headers), timeout_seconds))
        return self.resp


def _fetcher(resp):
    return _RecordingFetcher(resp)


def _raising_fetcher(error):
    def fetcher(url, headers, timeout_seconds):
        raise error

    return fetcher


class TestFetchDecodeDiscovery:
    """The shared core: one place for the fetch/cap/decode policy."""

    async def test_decodes_two_hundred_body(self):
        fetcher = _fetcher(_FakeResp(200, {"content-type": "text/plain"}, b"hello"))
        text, status, headers = await _fetch_decode_discovery(fetcher, "http://e.com/r", {}, 5)
        assert text == "hello"
        assert status == 200
        assert headers == {"content-type": "text/plain"}

    async def test_headers_are_lowercased(self):
        fetcher = _fetcher(_FakeResp(200, {"Content-Type": "text/plain", "ETag": '"1"'}, b"x"))
        _, _, headers = await _fetch_decode_discovery(fetcher, "http://e.com/r", {}, 5)
        assert headers == {"content-type": "text/plain", "etag": '"1"'}

    async def test_non_two_hundred_returns_no_body_but_status_and_headers(self):
        # The body is absent, but status + headers survive so the sitemap path
        # can still read ETag/Last-Modified off a 404/5xx.
        fetcher = _fetcher(_FakeResp(404, {"etag": '"1"'}, b""))
        text, status, headers = await _fetch_decode_discovery(fetcher, "http://e.com/r", {}, 5)
        assert text is None
        assert status == 404
        assert headers == {"etag": '"1"'}

    async def test_transport_failure_returns_all_none(self):
        fetcher = _raising_fetcher(ConnectionError("boom"))
        assert await _fetch_decode_discovery(fetcher, "http://e.com/r", {}, 5) == (None, None, None)

    async def test_coroutine_fetcher_is_awaited(self):
        async def fetcher(url, headers, timeout_seconds):
            return _FakeResp(200, {"content-type": "text/plain"}, b"async")

        text, status, _ = await _fetch_decode_discovery(fetcher, "http://e.com/r", {}, 5)
        assert text == "async"
        assert status == 200

    async def test_gzip_body_is_decompressed(self):
        body = gzip.compress(b"<urlset></urlset>")
        fetcher = _fetcher(
            _FakeResp(200, {"content-type": "application/xml", "content-encoding": "gzip"}, body)
        )
        text, status, _ = await _fetch_decode_discovery(fetcher, "http://e.com/s", {}, 5)
        assert text == "<urlset></urlset>"
        assert status == 200

    async def test_decompression_cap_returns_headers_without_body(self):
        # A gzip bomb past the cap: no text, but status + headers survive so the
        # sitemap path keeps its validators instead of losing them to the bomb.
        fetcher = _fetcher(
            _FakeResp(
                200,
                {"content-type": "text/plain", "content-encoding": "gzip", "etag": '"e"'},
                _gzip_bomb(),
            )
        )
        text, status, headers = await _fetch_decode_discovery(fetcher, "http://e.com/s", {}, 5)
        assert text is None
        assert status == 200
        assert headers is not None
        assert headers.get("etag") == '"e"'

    async def test_headers_passed_through_to_fetcher(self):
        fetcher = _fetcher(_FakeResp(200, {"content-type": "text/plain"}, b"x"))
        await _fetch_decode_discovery(fetcher, "http://e.com/r", {"if-none-match": '"1"'}, 9)
        assert fetcher.calls == [("http://e.com/r", {"if-none-match": '"1"'}, 9)]


class TestFetchAndDecodeContract:
    """robots.txt contract: the engine distinguishes 404 from 5xx (fail closed)."""

    async def test_two_hundred_returns_body_and_status(self):
        fetcher = _fetcher(_FakeResp(200, {"content-type": "text/plain"}, b"User-agent: *"))
        assert await fetch_and_decode(fetcher, "http://e.com/robots.txt", {}, 5) == ("User-agent: *", 200)

    async def test_missing_document_returns_none_with_status(self):
        fetcher = _fetcher(_FakeResp(404, {}, b""))
        assert await fetch_and_decode(fetcher, "http://e.com/robots.txt", {}, 5) == (None, 404)

    async def test_transport_failure_returns_none_none(self):
        fetcher = _raising_fetcher(ConnectionError("boom"))
        assert await fetch_and_decode(fetcher, "http://e.com/robots.txt", {}, 5) == (None, None)

    async def test_decompression_cap_returns_none_with_status(self):
        fetcher = _fetcher(_FakeResp(200, {"content-encoding": "gzip"}, _gzip_bomb()))
        assert await fetch_and_decode(fetcher, "http://e.com/robots.txt", {}, 5) == (None, 200)


class TestFetchTextContract:
    """Sitemap contract: 304 reuses cached state, validators always survive."""

    async def test_two_hundred_returns_text_and_validators(self):
        fetcher = _fetcher(
            _FakeResp(
                200,
                {"content-type": "application/xml", "etag": '"1"', "last-modified": "Tue"},
                b"<urlset/>",
            )
        )
        assert await _fetch_text(fetcher, "http://e.com/s.xml", None, 5) == ("<urlset/>", '"1"', "Tue", False)

    async def test_not_modified_reuses_validators_without_body(self):
        fetcher = _fetcher(_FakeResp(304, {"etag": '"1"', "last-modified": "Tue"}, b""))
        assert await _fetch_text(fetcher, "http://e.com/s.xml", None, 5) == (None, '"1"', "Tue", True)

    async def test_missing_document_returns_all_none(self):
        fetcher = _fetcher(_FakeResp(404, {"etag": '"1"'}, b""))
        assert await _fetch_text(fetcher, "http://e.com/s.xml", None, 5) == (None, None, None, False)

    async def test_transport_failure_returns_all_none(self):
        fetcher = _raising_fetcher(ConnectionError("boom"))
        assert await _fetch_text(fetcher, "http://e.com/s.xml", None, 5) == (None, None, None, False)

    async def test_decompression_cap_keeps_validators(self):
        bomb = gzip.compress(b"\x00" * (DISCOVERY_BODY_MAX_BYTES + 4096))
        fetcher = _fetcher(_FakeResp(200, {"content-encoding": "gzip", "etag": '"1"'}, bomb))
        assert await _fetch_text(fetcher, "http://e.com/s.xml", None, 5) == (None, '"1"', None, False)

    async def test_conditional_headers_are_passed(self):
        fetcher = _fetcher(_FakeResp(304, {"etag": '"1"'}, b""))
        await _fetch_text(fetcher, "http://e.com/s.xml", {"if-none-match": '"1"'}, 5)
        assert fetcher.calls == [("http://e.com/s.xml", {"if-none-match": '"1"'}, 5)]
