"""H1: the cffi fetcher must stream with a size cap, not buffer unbounded.

A hostile origin can stream gigabytes without a Content-Length header
(chunked). The cffi path read the entire body into memory before any cap
applied. These tests pin the fix: stream via the response queue, abort past
max_content_length with a 413, and stop consuming the stream promptly.
"""

from app.crawl.strategies.http import _default_cffi_fetcher


class _FeedOnDemandQueue:
    """Feeds one chunk per get(), mirroring curl's bounded-queue backpressure:
    the producer only advances when the consumer reads, so counters measure
    consumption (the property under test), not producer progress."""

    def __init__(self, body: bytes, chunk_size: int):
        self._body = body
        self._chunk_size = chunk_size
        self._offset = 0

    async def get(self):
        from curl_cffi.requests.models import STREAM_END

        if self._offset >= len(self._body):
            return STREAM_END
        chunk = self._body[self._offset : self._offset + self._chunk_size]
        self._offset += len(chunk)
        self.chunks_yielded = self._offset // self._chunk_size + (1 if self._offset % self._chunk_size else 0)
        return chunk


class _FakeCffiResponse:
    def __init__(self, body: bytes, *, chunk_size=1024):
        self.status_code = 200
        self.url = "http://example.test/page"
        self.headers = {}  # no content-length: chunked-style body
        self.elapsed = 0.01
        # The fetcher consumes queue items directly (the aiter_content
        # wrapper is bypassed because its aclose wedges on aborted streams).
        self.queue = _FeedOnDemandQueue(body, chunk_size)

    @property
    def content(self):
        # Emulates buffering: hands over the whole body at once.
        return b""

    @property
    def chunks_yielded(self):
        return self.queue.chunks_yielded


class _FakeCffiClient:
    last_instance = None

    def __init__(self, body: bytes, **kwargs):
        self._body = body
        self.response = None

    async def __aenter__(self):
        type(self).last_instance = self
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.response = _FakeCffiResponse(self._body)
        return self.response


def _patch_client(monkeypatch, body: bytes):
    import curl_cffi.requests as cffi_requests

    _FakeCffiClient.last_instance = None
    monkeypatch.setattr(cffi_requests, "AsyncSession", lambda **kw: _FakeCffiClient(body))


class TestCffiStreamingCap:
    async def test_chunked_body_over_cap_returns_413(self, monkeypatch):
        _patch_client(monkeypatch, b"x" * 10_000)
        result = await _default_cffi_fetcher("http://example.test/page", {}, 10, 64)
        assert result.status_code == 413

    async def test_real_client_class_exists(self):
        # Detection-gap guard: the cffi path once referenced AsyncClient,
        # which does not exist in curl_cffi>=0.7, so every impersonated
        # fetch silently fell back to plain httpx. Pin the real API.
        import curl_cffi.requests as cffi_requests

        assert hasattr(cffi_requests, "AsyncSession")

    async def test_streaming_stops_shortly_after_cap(self, monkeypatch):
        _patch_client(monkeypatch, b"x" * 100_000)
        result = await _default_cffi_fetcher("http://example.test/page", {}, 10, 64)
        assert result.status_code == 413
        consumed = _FakeCffiClient.last_instance.response.chunks_yielded * 1024
        assert consumed <= 64 + 1024
