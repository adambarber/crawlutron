"""Perf item 4 (RED): curl_cffi must use one shared AsyncSession per loop.

``_default_cffi_fetcher`` opens a fresh ``AsyncSession`` per request (new
TLS state + connection pool every time). It must mirror
``get_async_http_client``: one shared session reused while the event loop is
unchanged, transparently rebuilt when the loop changes.
"""

import asyncio


async def _fetch_twice(base_url):
    from app.crawl.strategies import http as http_mod

    async def one():
        return await http_mod._default_cffi_fetcher(base_url + "/", {}, 10, 1024 * 1024)

    r1 = await one()
    r2 = await one()
    return r1, r2


async def test_cffi_session_reused_across_requests(base_url, monkeypatch, real_sleeps):
    """Two sequential fetches must construct exactly one AsyncSession.

    Needs real wall-clock sleeps: curl_cffi's socket/timer watchdog uses
    ``asyncio.sleep`` and the suite's instant-sleep patch would starve it
    (same reason as ``test_http_cffi_import_fallback``).
    """
    import curl_cffi.requests as cffi_mod

    from app.crawl.strategies import http as http_mod

    real_session = cffi_mod.AsyncSession
    constructions = 0

    class _CountingSession(real_session):
        def __init__(self, *args, **kwargs):
            nonlocal constructions
            constructions += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(cffi_mod, "AsyncSession", _CountingSession)
    monkeypatch.setattr(http_mod, "_DEFAULT_CFFI_SESSION", None)
    try:
        r1, r2 = await _fetch_twice(base_url)
    finally:
        await http_mod.close_cffi_session()
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert constructions == 1


def test_cffi_session_rebuilt_on_loop_change(monkeypatch):
    """A new event loop gets a fresh session (no sockets bound to a dead loop)."""
    from app.crawl.strategies import http as http_mod

    async def _get():
        return http_mod.get_cffi_session()

    async def _close():
        await http_mod.close_cffi_session()

    s1 = asyncio.run(_get())
    assert s1 is not None
    s2 = asyncio.run(_get())
    try:
        assert s1 is not s2
    finally:
        asyncio.run(_close())


async def test_cffi_missing_falls_back_to_httpx(monkeypatch):
    """Without curl_cffi the fetcher still delegates to the plain httpx path."""
    import sys

    from app.crawl.strategies import http as http_mod

    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    monkeypatch.setattr(http_mod, "_DEFAULT_CFFI_SESSION", None)
    seen: list[str] = []

    async def fake_http(url, headers, timeout_seconds, max_content_length=0):
        seen.append(url)

        class Resp:
            status_code = 200
            headers = {"content-type": "text/html"}
            body = b"<html><body>fallback</body></html>"
            final_url = url
            elapsed_ms = 1

        return Resp()

    monkeypatch.setattr(http_mod, "_default_http_fetcher", fake_http)
    try:
        resp = await http_mod._default_cffi_fetcher("http://example.com/", {}, 5, 1024)
    finally:
        await http_mod.close_cffi_session()
    assert resp.status_code == 200
    assert seen == ["http://example.com/"]
