"""Adversarial fix (MEDIUM): HTTP client init uses get_running_loop, not the
deprecated get_event_loop, and is guarded against concurrent first-time build.
"""

import asyncio

import app.crawl.strategies.http as http_mod
from app.crawl.strategies.http import get_async_http_client


def test_get_async_http_client_avoids_deprecated_get_event_loop(monkeypatch):
    # Prove the code path no longer depends on the deprecated get_event_loop:
    # make it raise and confirm the client is still obtainable (it must use
    # get_running_loop instead).
    monkeypatch.setattr(
        asyncio,
        "get_event_loop",
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


def test_hot_path_client_reuse_skips_env_scan(monkeypatch):
    # Throughput (perf review 1.1): the per-fetch get_async_http_client() call
    # must not pay a full CrawlerSettings.from_mapping() env walk when the
    # pooled client is alive on the current loop. Settings are pool config
    # that is consumed only at construction, so a reuse call consults env zero
    # times while a construction call consults it exactly once.
    scans = {"count": 0}
    original = http_mod._default_settings

    def _counting_settings():
        scans["count"] += 1
        return original()

    monkeypatch.setattr(http_mod, "_default_settings", _counting_settings)
    monkeypatch.setattr(http_mod, "_DEFAULT_HTTP_CLIENT", None)

    async def inner():
        first = get_async_http_client()
        reuse_calls = scans["count"]
        second = get_async_http_client()
        third = get_async_http_client()
        return first, second is first, third is first, reuse_calls, scans["count"]

    first, reuses_second, reuses_third, reuse_calls, total = asyncio.run(inner())
    assert first is not None
    assert reuses_second and reuses_third
    # Construction walked the env exactly once; the two reuse calls added nothing.
    assert total == 1
    assert reuse_calls == 1
