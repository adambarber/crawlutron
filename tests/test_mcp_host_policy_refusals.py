"""Every MCP host-policy entry point refuses a blocked URL without side effects."""

from __future__ import annotations

from fastapi import HTTPException

from tests._host_policy_probes import (
    _BLOCKED_URL,
    _assert_refused,
    _denied_settings,
)


def _blocked_http(*args, **kwargs):
    raise HTTPException(status_code=422, detail="blocked by host policy")


def _tool_payload(tool: str, **fields):
    """Build what the MCP dispatcher builds before a handler ever sees it.

    The dispatcher validates a call's arguments into the tool's declared
    ``request_model``, so a probe that invokes a handler directly has to
    construct that same model -- a raw dict is no longer what a handler takes.
    Reading the model off the registration rather than naming it here keeps the
    probe from becoming a second place that knows which model a tool uses.
    """
    from app.mcp.tools import TOOLS_BY_NAME

    return TOOLS_BY_NAME[tool].request_model(**fields)


async def _probe_mcp_crawl(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "execute_crawl", _blocked_http)
    await _assert_refused(
        tools._handle_crawl(_tool_payload("crawl", url=_BLOCKED_URL), _denied_settings(), None), {}
    )


async def _probe_mcp_crawl_and_extract(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "execute_crawl", _blocked_http)
    await _assert_refused(
        tools._handle_crawl_and_extract(
            _tool_payload("crawl_and_extract", url=_BLOCKED_URL), _denied_settings(), None
        ),
        {},
    )


async def _probe_mcp_map(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "map_site", _blocked_http)
    await _assert_refused(
        tools._handle_map(_tool_payload("map", url=_BLOCKED_URL), _denied_settings(), None), {}
    )


async def _probe_mcp_render(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "get_browser_pool", lambda: object())
    monkeypatch.setattr(tools, "render_endpoint_handler", _blocked_http)
    await _assert_refused(
        tools._handle_render(_tool_payload("render", url=_BLOCKED_URL), _denied_settings(), None), {}
    )


async def _probe_mcp_search(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "search_endpoint_handler", _blocked_http)
    await _assert_refused(
        tools._handle_search(_tool_payload("search", query="blocked"), _denied_settings(), None), {}
    )
