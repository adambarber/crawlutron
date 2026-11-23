"""MCP crawl/map/render effects run under EffectContainer, so in-flight effects
are bounded by the per-kind semaphore width and a slow render never holds an
admission slot for other callers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx import AsyncClient

from crawlutron.service.effects import EffectContainer, EffectKind
from crawlutron.service.errors import QUOTA_EXCEEDED_MESSAGE
from tests._app_harnesses import mounted_app
from tests._async_harnesses import issue_key
from tests._quota_harness import mcp_crawl_keys_routers, service_client
from tests.mcp._tool_calls import tool_call


class _EffectProbe:
    """Records in-flight effect counts per kind from the stubbed doors."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {kind: 0 for kind in (EffectKind.FETCH, EffectKind.RENDER)}
        self.max: dict[str, int] = {kind: 0 for kind in (EffectKind.FETCH, EffectKind.RENDER)}
        self.crawl_stub = self._make_fetch_stub(
            {"results": [], "request_id": "req", "submitted_count": 0, "policy_schema_version": ""}
        )
        self.map_stub = self._make_fetch_stub({"all_urls": []})
        self.render_stub = self._render_stub

    def _track(self, kind: EffectKind) -> None:
        self.counts[kind] += 1
        self.max[kind] = max(self.max[kind], self.counts[kind])

    def _untrack(self, kind: EffectKind) -> None:
        self.counts[kind] -= 1

    def _make_fetch_stub(self, result: object):
        """A FETCH-kind stub that holds briefly so concurrent effects overlap."""

        async def _stub(*args, **kwargs):
            self._track(EffectKind.FETCH)
            try:
                await asyncio.sleep(0.05)
                return result
            finally:
                self._untrack(EffectKind.FETCH)

        return _stub

    async def _render_stub(self, *args, **kwargs):
        self._track(EffectKind.RENDER)
        try:
            await asyncio.sleep(0.05)
            return {}
        finally:
            self._untrack(EffectKind.RENDER)


def _stub_effects(monkeypatch, probe, render_stub) -> None:
    """Stub the crawl/map/render doors and the browser-pool guard."""
    monkeypatch.setattr("crawlutron_mcp.handlers.execute_crawl", probe.crawl_stub)
    monkeypatch.setattr("crawlutron_mcp.handlers.map_site", probe.map_stub)
    monkeypatch.setattr("crawlutron_mcp.handlers.render_endpoint_handler", render_stub)
    monkeypatch.setattr("crawlutron_mcp.handlers.get_browser_pool", lambda: object())


@asynccontextmanager
async def _mcp_client(tmp_path, *, fetch_width: int, render_width: int) -> AsyncGenerator[AsyncClient]:
    """An MCP client whose app carries an EffectContainer of the given widths."""
    async with service_client(tmp_path, routers=mcp_crawl_keys_routers(), with_results=True) as (
        client,
        _job_store,
        _keys,
    ):
        app = mounted_app(client)
        app.state.effect_container = EffectContainer(
            concurrency_per_kind={EffectKind.FETCH: fetch_width, EffectKind.RENDER: render_width}
        )
        yield client


@pytest.mark.real_sleeps
async def test_concurrent_mcp_effects_bounded_by_container_width(tmp_path, monkeypatch):
    """Concurrent crawl/map/render effects stay within the container's per-kind
    semaphore width; the quota slot is released after admission, so it never bounds
    effect concurrency."""
    probe = _EffectProbe()
    _stub_effects(monkeypatch, probe, probe.render_stub)

    async with _mcp_client(tmp_path, fetch_width=2, render_width=1) as client:
        secret = await issue_key(client, max_pages=0, max_concurrency=0)

        results = await asyncio.gather(
            tool_call(client, "crawl", {"url": "http://example.com/a"}, key=secret),
            tool_call(client, "map", {"url": "http://example.com/b"}, key=secret),
            tool_call(client, "render", {"url": "http://example.com/c"}, key=secret),
            tool_call(client, "crawl", {"url": "http://example.com/d"}, key=secret),
        )

    for result in results:
        assert result["isError"] is False, result

    assert probe.max[EffectKind.FETCH] <= 2
    assert probe.max[EffectKind.RENDER] <= 1


async def test_slow_render_holds_quota_slot_for_same_caller(tmp_path, monkeypatch):
    """A slow render holds the quota slot while its effect runs: a same-key crawl
    is refused (REST parity) while a different-key crawl is not blocked."""
    entered = asyncio.Event()
    released = asyncio.Event()

    async def slow_render(*args, **kwargs):
        entered.set()
        await released.wait()
        return {}

    probe = _EffectProbe()
    _stub_effects(monkeypatch, probe, slow_render)

    async with _mcp_client(tmp_path, fetch_width=1, render_width=1) as client:
        slow_secret = await issue_key(client, max_pages=0, max_concurrency=1)
        fast_secret = await issue_key(client, max_pages=0, max_concurrency=1)

        render_task = asyncio.create_task(
            tool_call(client, "render", {"url": "http://example.com/slow"}, key=slow_secret)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        # Same caller: the slot is held across the render's effect, so a same-key
        # crawl is refused instead of running concurrently.
        crawl_result = await asyncio.wait_for(
            tool_call(client, "crawl", {"url": "http://example.com/fast"}, key=slow_secret),
            timeout=5,
        )
        assert crawl_result["isError"] is True, crawl_result
        assert crawl_result["content"][0]["text"] == QUOTA_EXCEEDED_MESSAGE, crawl_result
        assert crawl_result["structuredContent"]["code"] == "quota_exceeded", crawl_result

        # Different caller: not blocked by the slow render's held slot.
        fast_result = await asyncio.wait_for(
            tool_call(client, "crawl", {"url": "http://example.com/other"}, key=fast_secret),
            timeout=5,
        )
        assert fast_result["isError"] is False, fast_result

        released.set()
        render_result = await asyncio.wait_for(render_task, timeout=5)
        assert render_result["isError"] is False, render_result


async def test_same_key_concurrent_crawls_do_not_both_run(tmp_path, monkeypatch):
    """With max_concurrency=1, two concurrent same-key MCP crawls must not both
    run; the second is paced/refused while the first holds the quota slot."""
    entered = asyncio.Event()
    released = asyncio.Event()
    crawl_result = {"results": [], "request_id": "req", "submitted_count": 0, "policy_schema_version": ""}

    async def slow_crawl(*args, **kwargs):
        entered.set()
        await released.wait()
        return crawl_result

    probe = _EffectProbe()
    _stub_effects(monkeypatch, probe, probe.render_stub)
    monkeypatch.setattr("crawlutron_mcp.handlers.execute_crawl", slow_crawl)

    async with _mcp_client(tmp_path, fetch_width=2, render_width=1) as client:
        secret = await issue_key(client, max_pages=0, max_concurrency=1)

        first = asyncio.create_task(tool_call(client, "crawl", {"url": "http://example.com/a"}, key=secret))
        await asyncio.wait_for(entered.wait(), timeout=5)

        second = asyncio.create_task(tool_call(client, "crawl", {"url": "http://example.com/b"}, key=secret))
        second_result = await asyncio.wait_for(second, timeout=5)
        assert second_result["isError"] is True, second_result
        assert second_result["content"][0]["text"] == QUOTA_EXCEEDED_MESSAGE, second_result
        assert second_result["structuredContent"]["code"] == "quota_exceeded", second_result

        released.set()
        first_result = await asyncio.wait_for(first, timeout=5)
        assert first_result["isError"] is False, first_result
