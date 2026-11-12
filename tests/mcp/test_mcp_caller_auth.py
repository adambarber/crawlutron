"""MCP caller-auth behavior: an invalid credential is a 401, never a benign 200 (#205)."""

from __future__ import annotations

import pytest

from tests.mcp.test_mcp import _META as META
from tests.mcp.test_mcp import _body, _error_of
from tests.mcp.test_mcp import _mcp as mcp_post


def _assert_401_api_key(resp):
    """Assert the response is a 401 with an api-key error message."""
    assert resp.status_code == 401, resp.text
    assert "api key" in str(_error_of(resp)["message"]).lower()


class TestCallerAuth:
    @pytest.mark.usefixtures("browser_pool_stub")
    @pytest.mark.parametrize("method", ["crawl", "discover"])
    async def test_a_wrong_key_is_a_401_not_a_benign_200(self, async_client, base_url, method):
        """The route refuses an invalid credential for every method, not just tools/call."""
        body = (
            _body("tools/call", params={"name": "crawl", "arguments": {"url": f"{base_url}/"}, "_meta": META})
            if method == "crawl"
            else _body("server/discover")
        )
        resp = await mcp_post(async_client, body, {"X-API-Key": "definitely-not-a-key"})
        _assert_401_api_key(resp)


async def test_an_unwired_mcp_port_refuses_loudly():
    """An MCP app built without the stash is refused 500 caller_quota_unwired
    instead of being admitted through the silent singleton default."""
    import asyncio

    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
    from crawlutron_rest.api.mcp_router import router as mcp_router
    from tests._app_harnesses import harness_app
    from tests._async_harnesses import serve

    app = harness_app(mcp_router)
    app.state.crawler_settings = CrawlerSettings.from_mapping({})
    app.state.shutdown_event = asyncio.Event()
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    del app.state.caller_quota_admission
    async with serve(app) as client:
        body = _body(
            "tools/call",
            params={"name": "search", "arguments": {"query": "x"}, "_meta": META},
        )
        resp = await mcp_post(client, body)
        assert resp.status_code == 500, resp.text
        assert resp.json()["code"] == "caller_quota_unwired", resp.text
