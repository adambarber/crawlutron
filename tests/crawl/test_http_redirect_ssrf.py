import httpx
import pytest

import app.crawl.strategies.http as http_mod


class _Resp:
    """Minimal fake httpx response usable as an async-stream context body."""

    def __init__(self, status, headers, url, chunks=()):
        self.status_code = status
        self.headers = httpx.Headers(headers)
        self.url = url
        self._chunks = list(chunks)

    def aiter_raw(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        # record (url, follow_redirects) per stream() invocation
        self.calls = []
        self._i = 0

    def stream(self, method, url, headers=None, timeout=None, follow_redirects=False):
        self.calls.append((url, follow_redirects))
        resp = self._responses[self._i]
        self._i += 1
        return _Ctx(resp)


async def test_redirect_to_unsafe_loopback_is_not_followed(monkeypatch):
    """F1: a 302 to an internal (loopback) target must NOT be fetched.

    The original code used follow_redirects=True, so httpx would connect to
    the redirect target without re-checking it -> SSRF. The fix follows
    redirects manually and re-runs _build_pinned_request (which validates the
    target against the SSRF blocklist) on every hop, so an unsafe hop raises.
    """
    responses = [
        _Resp(302, {"location": "http://127.0.0.1:9/secret"}, "http://public.example/"),
    ]
    fake = _FakeClient(responses)
    monkeypatch.setattr(http_mod, "get_async_http_client", lambda *a, **k: fake)

    # Simulate the SSRF check: block anything pointing at loopback.
    def fake_build(url, headers):
        if "127.0.0.1" in url:
            raise ValueError(f"SSRF: blocked target {url}")
        return url, dict(headers)

    monkeypatch.setattr(http_mod, "_build_pinned_request", fake_build)

    with pytest.raises(ValueError):
        await http_mod._default_http_fetcher("http://public.example/", {}, 30)

    # Only the initial (safe) URL was fetched; the unsafe redirect target was
    # validated and rejected before any second request was issued.
    assert [url for url, _ in fake.calls] == ["http://public.example/"]
    # Manual redirect handling must disable automatic httpx following.
    assert all(fr is False for _, fr in fake.calls)


async def test_redirect_to_safe_target_is_followed(monkeypatch):
    """A redirect to another safe public host IS followed (within hop budget)."""
    responses = [
        _Resp(302, {"location": "http://other.example/"}, "http://public.example/"),
        _Resp(200, {"content-type": "text/html"}, "http://other.example/", chunks=[b"<html>ok</html>"]),
    ]
    fake = _FakeClient(responses)
    monkeypatch.setattr(http_mod, "get_async_http_client", lambda *a, **k: fake)
    # Treat every host as safe so the loop can follow.
    monkeypatch.setattr(http_mod, "_build_pinned_request", lambda url, headers: (url, dict(headers)))

    result = await http_mod._default_http_fetcher("http://public.example/", {}, 30)

    assert result.status_code == 200
    assert result.body == b"<html>ok</html>"
    # Initial request + one followed redirect.
    assert [url for url, _ in fake.calls] == ["http://public.example/", "http://other.example/"]
    assert all(fr is False for _, fr in fake.calls)


async def test_redirect_loop_is_bounded_by_hop_limit(monkeypatch):
    """The manual loop must terminate even on indefinite redirects (F1)."""
    responses = [_Resp(302, {"location": "http://a.example/"}, "http://public.example/") for _ in range(10)]
    fake = _FakeClient(responses)
    monkeypatch.setattr(http_mod, "get_async_http_client", lambda *a, **k: fake)
    monkeypatch.setattr(http_mod, "_build_pinned_request", lambda url, headers: (url, dict(headers)))

    result = await http_mod._default_http_fetcher("http://public.example/", {}, 30)

    # Terminates at the hop cap and returns the last 302 rather than looping.
    assert result.status_code == 302
    assert len(fake.calls) == http_mod._MAX_REDIRECT_HOPS + 1
