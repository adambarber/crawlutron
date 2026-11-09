"""The MCP watch-tool handlers: register/list/get/delete over the watch cores.

Split from handlers.py for the 500-line ceiling (#648). The watch tools are
store reads/writes, not fetch executors -- the caller scope is the resolved
caller, ungated by quota -- and the register door is poll-only: webhook
delivery is admitted through the REST door POST /v1/watches only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawlutron.api.watch_cores import (
    delete_watch_core,
    get_watch_core,
    list_watches_core,
    register_watch_core,
)
from crawlutron.api.watch_router import WATCH_INTERVAL_MIN_SECONDS
from crawlutron.mcp.handlers import Handler, ToolResult, caller_for, tool_result

if TYPE_CHECKING:
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.models.watch_create_request import WatchCreateRequest

__all__ = [
    "handle_delete_watch",
    "handle_get_watch",
    "handle_list_watches",
    "handle_register_watch",
]


_WEBHOOK_REFUSAL = ToolResult(
    text="webhook_url is not supported over MCP: webhook delivery is admitted "
    "through the REST door POST /v1/watches only; use it without webhook_url "
    "for a poll-only watch.",
    is_error=True,
)


async def register_watch_tool_core(
    payload: WatchCreateRequest, watch_store, settings, caller: CallerIdentity
):
    """The poll-only creation core: MCP registers never carry webhook delivery."""
    return await register_watch_core(
        payload,
        watch_store,
        settings,
        caller,
        webhook_url=None,
        interval_floor=WATCH_INTERVAL_MIN_SECONDS,
    )


def _watch_tool_handler(core, *, refusal=None) -> Handler:
    """One watch-tool handler over a store-read core: the watch tools differ
    only by the core they run, so the handler boilerplate is spelled once here
    rather than cloned per tool."""

    async def handler(
        payload,
        settings,
        shutdown_event,
        caller=None,
        watch_store=None,
        job_store=None,
        result_store=None,
        concurrency_tracker=None,
    ) -> ToolResult:
        if watch_store is None:
            raise ValueError("watch tools require an initialized watch store")
        if refusal is not None and getattr(payload, "webhook_url", None) is not None:
            return refusal
        return await tool_result(core(payload, watch_store, settings, caller_for(caller)))

    return handler


handle_register_watch = _watch_tool_handler(
    register_watch_tool_core,
    refusal=_WEBHOOK_REFUSAL,
)
handle_list_watches = _watch_tool_handler(list_watches_core)
handle_get_watch = _watch_tool_handler(get_watch_core)
handle_delete_watch = _watch_tool_handler(delete_watch_core)
