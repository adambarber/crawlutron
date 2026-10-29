"""#328/#344: every MCP fetch tool routes through request_gate like the HTTP doors.

The five fetch tools funnel through ``_admit_tool``; before #328 they ran ungated,
so /ready missed MCP work and the capacity cap refused none of it.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.api import main_helpers
from crawlutron.config.settings import CrawlerSettings
from crawlutron.health.readiness import ReadinessState
from crawlutron.mcp.tools import handle_crawl, handle_map, handle_render, handle_search
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.map_request import MapRequest
from crawlutron.models.render_request import RenderRequest
from crawlutron.models.search_request import SearchRequest

#: (label, handler, stub_target, payload, extra_patches) for the four fetch kinds.
#: crawl also covers crawl_and_extract, which shares ``_guard_then_crawl``.
_TOOL_CASES = [
    ("map", handle_map, "map_site", MapRequest(url="http://example.com/"), {}),
    (
        "render",
        handle_render,
        "render_endpoint_handler",
        RenderRequest(url="http://example.com/"),
        {"get_browser_pool": lambda: object()},
    ),
    ("search", handle_search, "search_endpoint_handler", SearchRequest(query="x"), {}),
    ("crawl", handle_crawl, "execute_crawl", CrawlRequest(url="http://example.com/"), {}),
]

_TOOL_IDS = [case[0] for case in _TOOL_CASES]


def _stub_for(label: str, active, held: list[int]):
    """A stubbed executor that records the readiness count alive during its run."""

    async def _record_render(payload, *, settings):
        del settings
        held.append(active())
        return {"results": [], "request_id": "req-1", "submitted_count": 0}

    async def _record_crawl(payload, *, settings, shutdown_event):
        del settings, shutdown_event
        held.append(active())
        envelope = {"results": [], "request_id": "req-1", "submitted_count": 0}
        envelope["policy_schema_version"] = ""
        return envelope

    async def _record_map(payload, *, settings, shutdown_event):
        del settings, shutdown_event
        held.append(active())
        return {"all_urls": []}

    async def _record_search(payload, *, settings, caller):
        del settings, caller
        held.append(active())
        return {"success": True, "data": []}

    if label == "map":
        return _record_map
    if label == "search":
        return _record_search
    if label == "render":
        return _record_render
    return _record_crawl


async def _invoke(monkeypatch, *, cap: int | None, label: str, prefill: int = 0):
    """Run one MCP fetch under a controlled readiness state.

    Returns ``(result, recorded_slots, state)``; ``prefill`` takes readiness slots
    before the fetch runs so a ``cap`` is already reached.
    """
    case = next(c for c in _TOOL_CASES if c[0] == label)
    _, handler, target, payload, extra = case
    state = ReadinessState(max_active_requests=cap)
    monkeypatch.setattr(main_helpers, "readiness_state", state)
    for _ in range(prefill):
        assert state.begin_request()

    def _active() -> int:
        return state.snapshot().active_requests

    held: list[int] = []
    exec_stub = _stub_for(label, _active, held)
    for name, value in extra.items():
        monkeypatch.setattr(f"crawlutron.mcp.tools.{name}", value)
    monkeypatch.setattr(f"crawlutron.mcp.tools.{target}", exec_stub)
    settings = CrawlerSettings.from_mapping({})
    shutdown = asyncio.Event()
    result = await handler(payload, settings, shutdown)
    return result, held, state


@pytest.mark.parametrize("label", _TOOL_IDS)
async def test_mcp_fetch_holds_one_readiness_slot_while_it_runs(monkeypatch, label):
    """A running MCP fetch is visible on /ready: it holds exactly one slot.

    #344's crawl contribution is the same assertion for the crawl door."""
    result, held, state = await _invoke(monkeypatch, cap=None, label=label)
    before = 0
    assert held == [before + 1], f"{label} ran without a readiness slot: {held}"
    assert result.is_error is False, result.text
    assert state.snapshot().active_requests == before


@pytest.mark.parametrize("label", _TOOL_IDS)
async def test_mcp_fetch_refuses_over_the_cap(monkeypatch, label):
    """At the cap an MCP fetch is refused with the capacity envelope and takes no slot."""
    result, _held, state = await _invoke(monkeypatch, cap=1, label=label, prefill=1)
    assert result.is_error is True, result
    assert "configured cap" in result.text
    assert state.snapshot().active_requests == 1  # the refused request never held a slot
