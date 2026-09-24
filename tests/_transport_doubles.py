from urllib.parse import urlparse


class MockResponse:
    def __init__(self, status_code, headers, content=b""):
        self.status_code = status_code
        self.headers = headers
        self._content = content
        self.url = "http://example.com/"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_raw(self):
        yield self._content


class MockClient:
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0
        self.captured_headers = []

    def stream(self, method, url, headers, timeout, follow_redirects):
        self.call_count += 1
        self.captured_headers.append(dict(headers))
        response = self.responses[self.call_count - 1]
        response.url = url
        return response


def patch_http_fetcher_transport(monkeypatch, responses):
    from app.crawl.strategies import http_client_pool as pool_mod
    from app.crawl.strategies import pinned_transport as pinned_mod

    mock_client = MockClient(responses)
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: mock_client)
    monkeypatch.setattr(pinned_mod, "_check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(
        pinned_mod,
        "_resolve_and_pin_host",
        lambda url: (urlparse(url).hostname, "1.2.3.4"),
    )
    return mock_client


__all__ = ["MockClient", "MockResponse", "patch_http_fetcher_transport"]
