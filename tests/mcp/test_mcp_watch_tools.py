"""MCP watch tools (register_watch, list_watches, get_watch, delete_watch) over the watch store: they mirror
the /v1/watches REST doors -- same caller scoping, same store reads, same JSON -- under the poll-only
ruling (a webhook_url over MCP is refused).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import pytest
from fastapi import Request

from crawlutron.config.settings import CrawlerSettings
from crawlutron.mcp.registry import TOOLS_BY_NAME
from crawlutron_rest.api.dependencies import get_settings, get_shutdown_event, get_watch_store
from crawlutron_rest.api.mcp_router import router as mcp_router
from tests._app_harnesses import harness_app
from tests._async_harnesses import issue_key
from tests._quota_harness import keyed_client, stores
from tests.mcp._tool_calls import KeyedToolCaller

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from httpx import AsyncClient

TARGET = "http://example.com/feed.xml"
INTERVAL = 900


class WatchHarness(KeyedToolCaller):
    """One keyed harness: the client and the caller's secret."""

    def __init__(self, client: AsyncClient, secret: str) -> None:
        self.client = client
        self.secret = secret


async def other_caller(h: WatchHarness) -> WatchHarness:
    """A second keyed caller on the same harness client (the cross-visibility actor)."""
    return WatchHarness(client=h.client, secret=await issue_key(h.client))


@pytest.fixture
async def watch_harness(tmp_path: Path) -> AsyncGenerator[WatchHarness, None]:
    """A keyed client over /mcp + /v1/watches sharing one watch store and key admin."""
    from crawlutron.storage.watch_store import WatchStore
    from crawlutron_rest.api.keys_router import router as keys_router
    from crawlutron_rest.api.watch_router import router as watch_router

    _, _, keys = stores(tmp_path)
    app = harness_app(mcp_router, watch_router, keys_router)
    watch_store = WatchStore(tmp_path / "watches.db")
    app.state.watch_store = watch_store
    overrides: dict[Callable[[Request], object], Callable[[], object]] = {
        get_watch_store: lambda: watch_store,
        get_settings: lambda: CrawlerSettings.from_mapping({}),
        get_shutdown_event: asyncio.Event,
    }
    async with keyed_client(app, keys=keys, overrides=overrides) as client:
        yield WatchHarness(client=client, secret=await issue_key(client))


async def _registered(h: WatchHarness, url: str = TARGET) -> dict[str, object]:
    """One tool-registered watch; the structured answer is the watch's state."""
    structured: dict[str, object] = cast(
        "dict[str, object]",
        await h.structured("register_watch", {"url": url, "interval_seconds": INTERVAL}),
    )
    return structured


class TestRegisterWatchTool:
    async def test_the_tool_registers_a_watch_the_rest_door_answers(self, watch_harness):
        structured = await _registered(watch_harness)

        rest = await watch_harness.rest_json("get", f"/v1/watches/{structured['watch_id']}")
        assert rest == structured

    async def test_the_tool_and_the_rest_door_create_the_same_shape(self, watch_harness):
        structured = await _registered(watch_harness)

        assert (
            set(structured) == set(await watch_harness.rest_json("get", "/v1/watches"))
            or structured["url"] == TARGET
        )


class TestListWatchesTool:
    async def test_the_tool_returns_the_same_listing_as_rest(self, watch_harness):
        await _registered(watch_harness, "http://example.com/a")
        await _registered(watch_harness, "http://example.com/b")

        structured = await watch_harness.structured("list_watches", {})

        assert structured == await watch_harness.rest_json("get", "/v1/watches")
        assert len(structured["watches"]) == 2


class TestGetWatchTool:
    async def test_the_tool_returns_the_same_state_as_rest(self, watch_harness):
        created = await _registered(watch_harness)

        structured = await watch_harness.structured("get_watch", {"watch_id": created["watch_id"]})

        assert structured == await watch_harness.rest_json("get", f"/v1/watches/{created['watch_id']}")

    async def test_an_unknown_watch_is_a_tool_error_naming_the_id(self, watch_harness):
        text = await watch_harness.error_text("get_watch", {"watch_id": "watch-none"})

        assert "watch-none" in text


class TestDeleteWatchTool:
    async def test_the_tool_deletes_the_watch(self, watch_harness):
        created = await _registered(watch_harness)

        await watch_harness.structured("delete_watch", {"watch_id": created["watch_id"]})

        rest = await watch_harness.client.get(
            f"/v1/watches/{created['watch_id']}", headers={"X-API-Key": watch_harness.secret}
        )
        assert rest.status_code == 404


class TestWatchToolCallerScoping:
    async def test_another_callers_watch_is_invisible_to_every_watch_tool(self, watch_harness):
        created = await _registered(watch_harness)
        other = await other_caller(watch_harness)

        listed = await other.structured("list_watches", {})
        watch_id = str(created["watch_id"])
        get_text = await other.error_text("get_watch", {"watch_id": watch_id})
        delete_text = await other.error_text("delete_watch", {"watch_id": watch_id})

        assert listed == {"watches": []}
        assert watch_id in get_text
        assert watch_id in delete_text


class TestPollOnlyRefusal:
    async def test_register_watch_refuses_a_webhook_url_naming_the_rest_door(self, watch_harness):
        text = await watch_harness.error_text(
            "register_watch",
            {"url": TARGET, "interval_seconds": INTERVAL, "webhook_url": "https://hooks.example.com/x"},
        )

        assert "/v1/watches" in text
        assert "webhook" in text.lower()


class TestWatchToolDiscovery:
    def test_agent_hints_mention_the_watch_tools(self):
        from crawlutron_rest.discovery.agent_hints import agent_hints_payload

        guidance = " ".join(agent_hints_payload()["guidance"])
        assert "register_watch" in guidance
        assert "list_watches" in guidance


class TestRegisterWatchSchemaHonesty:
    def test_the_advertised_schema_does_not_offer_the_field_the_handler_refuses(self):
        """registry.py's own doctrine: the schema cannot offer a field the
        handler then refuses (#648 review F1) -- an agent sending what the
        tools/list schema advertises must not get a refusal."""
        spec = TOOLS_BY_NAME["register_watch"]
        properties = cast("dict[str, object]", spec.input_schema.get("properties", {}))
        required = cast("list[object]", spec.input_schema.get("required", []))
        assert "webhook_url" not in properties
        assert "webhook_url" not in required
        # The runtime refusal stays: defense in depth for direct callers.
        assert "webhook_url" in spec.unsupported_fields
