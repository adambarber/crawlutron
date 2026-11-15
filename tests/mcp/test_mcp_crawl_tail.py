"""MCP crawl tail: completion metric + result-store put, shared with REST."""

from __future__ import annotations

import pytest
from crawlutron_mcp.handlers import dispatch as tool_dispatch
from crawlutron_mcp.registry import TOOLS_BY_NAME

from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.runtime.metrics import get_metrics
from crawlutron.storage.result_store import CrawlResultStore


@pytest.fixture
def result_store(tmp_path):
    """ResultStore backed by a per-test SQLite db under ``tmp_path``."""
    from crawlutron.storage.safe_db import allow_on_loop

    store = CrawlResultStore(tmp_path / "results.db")
    with allow_on_loop():
        yield store
    store.close()


async def _mcp_crawl(job_store, result_store, monkeypatch):
    """Run one MCP crawl with the engine stubbed; return the ToolResult."""
    envelope = {
        "request_id": "req_mcp_tail",
        "results": [],
        "submitted_count": 0,
        "policy_schema_version": "",
    }

    async def _fake_execute(*args, **kwargs):
        return envelope

    monkeypatch.setattr("crawlutron_mcp.handlers.execute_crawl", _fake_execute)
    payload = CrawlRequest(url="http://example.com/")
    return await tool_dispatch(
        TOOLS_BY_NAME["crawl"],
        payload,
        settings=None,
        shutdown_event=None,
        job_store=job_store,
        result_store=result_store,
    )


async def test_mcp_crawl_leaves_a_replayable_result_store_row(job_store, result_store, monkeypatch):
    """An MCP crawl must persist its response so the replay door serves it by request_id."""
    result = await _mcp_crawl(job_store, result_store, monkeypatch)
    assert result.is_error is False
    assert await result_store.aget("req_mcp_tail") is not None


async def test_mcp_crawl_emits_the_completion_metric(job_store, result_store, monkeypatch):
    """An MCP crawl must move crawl_requests_total like the REST door does."""
    before = (await get_metrics().snapshot()).crawl_requests_total
    await _mcp_crawl(job_store, result_store, monkeypatch)
    after = (await get_metrics().snapshot()).crawl_requests_total
    assert after == before + 1
