"""H1: the cffi fetcher must stream with a size cap, not buffer unbounded.

A hostile origin can stream gigabytes without a Content-Length header
(chunked). The cffi path read the entire body into memory before any cap
applied. These tests pin the fix: stream via aiter_content, abort past
max_content_length with a 413, and stop consuming the stream promptly.
"""

from app.crawl.strategies.http import _default_cffi_fetcher


class _FakeCffiResponse:
    def __init__(self, body: bytes, *, chunk_size=1024):
        self._body = body
        self._chunk_size = chunk_size
        self.chunks_yielded = 0
        self.status_code = 200
        self.url = "http://example.test/page"
        self.headers = {}  # no content-length: chunked-style body
        self.elapsed = 0.01

    @property
    def content(self):
        # Emulates buffering: hands over the whole body at once.
        return self._body

    async def aiter_content(self, chunk_size=None):
        step = chunk_size or self._chunk_size
        for offset in range(0, len(self._body), step):
            self.chunks_yielded += 1
            yield self._body[offset : offset + step]


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
