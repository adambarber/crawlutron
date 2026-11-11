"""#320/#205: the MCP crawl door enforces the one concurrency ceiling.

The one per-caller register is threaded into every door through one admit helper, and
MCP crawl + HTTP sync crawl of the same caller share one ceiling (previously disjoint)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from crawlutron.storage.job_store import JobStatus
from tests._async_harnesses import issue_key
from tests._concurrency_probe import block_execute_crawl
from tests._quota_harness import service_client

PROTOCOL = "2026-07-28"


@contextlib.asynccontextmanager
async def _mcp_client(tmp_path: Path):
    """Isolated app serving /mcp + /v1/crawl + /v1/keys over one register and job store."""
    from tests._quota_harness import mcp_crawl_keys_routers

    async with service_client(
        tmp_path,
        routers=mcp_crawl_keys_routers(),
        with_results=True,
    ) as (client, job_store, keys):
        yield client, job_store, keys


def _mcp_headers(*, key: str) -> dict[str, str]:
    return {
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "crawl",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": key,
    }


def _mcp_crawl_body(url: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "crawl",
            "arguments": {"url": url},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


async def _mcp_crawl(client, *, key: str, url: str):
    """POST one MCP crawl for ``url`` with ``key``; returns ``(response, result)``."""
    response = await client.post(
        "/mcp",
        json=json.loads(json.dumps(_mcp_crawl_body(url))),
        headers=_mcp_headers(key=key),
    )
    if response.status_code != 200:
        return response, None
    return response, response.json()["result"]


def _assert_quota_refused(response, result) -> None:
    """The MCP quota-refusal shape: a 200 JSON-RPC with an isError tool result."""
    assert response.status_code == 200, response.text
    assert result["isError"] is True, result
    assert "concurrency quota" in result["content"][0]["text"]


async def test_keyed_mcp_crawl_refused_when_holding_an_async_job(tmp_path):
    """A max_concurrency=1 caller holding one live job row is refused on MCP crawl.

    MCP crawl must count durable job rows plus sync in-flight against one ceiling, so a
    caller cannot run its MCP quota fresh while it holds jobs (the register-unification anchor)."""
    async with _mcp_client(tmp_path) as (client, job_store, keys):
        secret = await issue_key(client, max_concurrency=1)
        record = keys.verify_key(secret)
        assert record is not None
        job_store.create_single_shot("busy", "http://example.com/", {}, owner=record.key_id)
        assert job_store.set_status("busy", JobStatus.RUNNING, expected=JobStatus.PENDING) is True

        _assert_quota_refused(*await _mcp_crawl(client, key=secret, url="http://example.com/admin"))


async def test_mcp_crawl_and_http_sync_crawl_share_one_ceiling(tmp_path, monkeypatch):
    """A sync crawl in flight over HTTP leaves the same caller's MCP crawl refused."""
    from crawlutron.api import crawl_endpoint as crawl_mod

    _, entered, released = block_execute_crawl(monkeypatch, crawl_mod)
    async with _mcp_client(tmp_path) as (client, job_store, keys):
        secret = await issue_key(client, max_concurrency=1)
        # Park an HTTP sync crawl so it holds the caller's one slot in the shared register.
        first = asyncio.create_task(
            client.post("/v1/crawl", headers={"X-API-Key": secret}, json={"url": "http://example.com/"})
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        try:
            _assert_quota_refused(*await _mcp_crawl(client, key=secret, url="http://example.com/other"))
        finally:
            released.set()
        await asyncio.wait_for(first, timeout=5)
