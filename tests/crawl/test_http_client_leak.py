'''MEDIUM fix: get_async_http_client must close the old pooled client
when the event loop changes, instead of leaking its sockets/fds.

When asyncio.run is called repeatedly (test suites, some service patterns),
the loop bound to the cached client changes; the old client was overwritten
without being closed. This test verifies the superseded client is closed.
'''
import asyncio

import pytest

import app.crawl.strategies.http as http_mod


class _FakeClient:
    def __init__(self):
        self.is_closed = False
        self._bound_loop = None

    async def aclose(self):
        self.is_closed = True


def _fake_builder(settings):
    client = _FakeClient()
    try:
        client._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        client._bound_loop = None
    return client


async def _run():
    return http_mod.get_async_http_client()


def test_superseded_client_is_closed_on_loop_change(monkeypatch):
    monkeypatch.setattr(http_mod, "_build_async_client", _fake_builder)
    http_mod._DEFAULT_HTTP_CLIENT = None
    try:
        c1 = asyncio.run(_run())           # builds on loop A
        c2 = asyncio.run(_run())           # new loop B -> old client closed
        assert c1.is_closed is True, "superseded HTTP client was leaked"
        assert c2.is_closed is False
    finally:
        http_mod._DEFAULT_HTTP_CLIENT = None
