"""Adversarial fix (MEDIUM): HTTP client init uses get_running_loop, not the
deprecated get_event_loop, and is guarded against concurrent first-time build.
"""
import asyncio

import pytest

import app.crawl.strategies.http as http_mod
from app.crawl.strategies.http import get_async_http_client


def test_get_async_http_client_avoids_deprecated_get_event_loop(monkeypatch):
    # Prove the code path no longer depends on the deprecated get_event_loop:
    # make it raise and confirm the client is still obtainable (it must use
    # get_running_loop instead).
    monkeypatch.setattr(
        asyncio, "get_event_loop",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("get_event_loop is deprecated")),
    )
    # Force the construction path.
    monkeypatch.setattr(http_mod, "_DEFAULT_HTTP_CLIENT", None)

    async def inner():
        return get_async_http_client()

    client = asyncio.run(inner())
    assert client is not None
    assert not client.is_closed


def test_get_async_http_client_reuses_same_instance(monkeypatch):
    monkeypatch.setattr(http_mod, "_DEFAULT_HTTP_CLIENT", None)

    async def inner():
        return get_async_http_client(), get_async_http_client()

    a, b = asyncio.run(inner())
    assert a is b
