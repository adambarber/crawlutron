"""MCP job tools (list_jobs, get_job, control_job, delete_job) over the job store.

The tools mirror the /v1/jobs REST doors: the same caller scoping, the same
store reads, and -- for the reads and the control action -- the same JSON the
REST door answers with.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from crawlutron.api.jobs_router import router as jobs_router
from crawlutron.api.keys_router import router as keys_router
from crawlutron.mcp.router import router as mcp_router
from crawlutron.models.json_value import JsonValue
from crawlutron.storage.job_store import JobStatus
from tests._async_harnesses import issue_key
from tests._quota_harness import service_client

if TYPE_CHECKING:
    from httpx import AsyncClient

    from crawlutron.storage.caller_key_store import CallerKeyStore
    from crawlutron.storage.job_store import JobStore

PROTOCOL = "2026-07-28"

_META: dict[str, JsonValue] = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@contextlib.asynccontextmanager
async def _job_mcp_client(
    tmp_path: Path,
) -> AsyncGenerator[tuple[AsyncClient, JobStore, CallerKeyStore], None]:
    """Isolated app serving /mcp + /v1/jobs over one job store and key admin."""
    async with service_client(
        tmp_path,
        routers={"mcp": mcp_router, "jobs": jobs_router, "keys": keys_router},
    ) as (client, job_store, keys):
        yield client, job_store, keys


def _mcp_headers(*, key: str, tool: str) -> dict[str, str]:
    return {
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool,
        "Accept": "application/json, text/event-stream",
        "X-API-Key": key,
    }


def _call_body(tool: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments, "_meta": _META},
    }


async def _tool_call(client: AsyncClient, tool: str, arguments: dict[str, JsonValue], *, key: str):
    """One keyed tools/call; returns the raw JSON-RPC result object."""
    response = await client.post(
        "/mcp",
        json=json.loads(json.dumps(_call_body(tool, arguments))),
        headers=_mcp_headers(key=key, tool=tool),
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


async def _tool_structured(client: AsyncClient, tool: str, arguments: dict[str, JsonValue], *, key: str):
    result = await _tool_call(client, tool, arguments, key=key)
    assert result["isError"] is False, result
    return result["structuredContent"]


async def _tool_error_text(
    client: AsyncClient, tool: str, arguments: dict[str, JsonValue], *, key: str
) -> str:
    result = await _tool_call(client, tool, arguments, key=key)
    assert result["isError"] is True, result
    return str(result["content"][0]["text"])


@dataclass(frozen=True)
class JobHarness:
    """One keyed harness: the client, the store, and the caller's secret."""

    client: AsyncClient
    job_store: object
    secret: str

    async def structured(self, tool: str, arguments: dict[str, JsonValue]):
        return await _tool_structured(self.client, tool, arguments, key=self.secret)

    async def error_text(self, tool: str, arguments: dict[str, JsonValue]) -> str:
        return await _tool_error_text(self.client, tool, arguments, key=self.secret)

    async def rest_json(self, method: str, path: str):
        """The REST door's JSON for one path, asserted 200 first."""
        rest = await getattr(self.client, method)(path, headers={"X-API-Key": self.secret})
        assert rest.status_code == 200, rest.text
        return rest.json()


@contextlib.asynccontextmanager
async def job_harness(tmp_path: Path, *, seeded: int = 0, running: bool = False):
    """A keyed harness with ``seeded`` single-shot jobs owned by the caller."""
    async with _job_mcp_client(tmp_path) as (client, job_store, keys):
        secret = await issue_key(client)
        record = keys.verify_key(secret)
        assert record is not None
        owner = record.key_id
        for index in range(seeded):
            job_id = f"job-{index}"
            job_store.create_single_shot(job_id, "http://example.com/", {}, owner=owner)
            if running:
                assert job_store.set_status(job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
        yield JobHarness(client=client, job_store=job_store, secret=secret)


class TestListJobsTool:
    async def test_the_tool_returns_the_same_page_as_rest(self, tmp_path):
        async with job_harness(tmp_path, seeded=2) as h:
            structured = await h.structured("list_jobs", {})
            assert structured == await h.rest_json("get", "/v1/jobs")
            assert structured["total"] == 2

    async def test_limit_and_offset_paginate_like_rest(self, tmp_path):
        async with job_harness(tmp_path, seeded=3) as h:
            structured = await h.structured("list_jobs", {"limit": 1, "offset": 1})
            assert structured == await h.rest_json("get", "/v1/jobs?limit=1&offset=1")
            assert structured["total"] == 3
            assert len(structured["jobs"]) == 1

    async def test_an_out_of_bounds_limit_is_a_tool_error_not_a_crash(self, tmp_path):
        async with job_harness(tmp_path) as h:
            text = await h.error_text("list_jobs", {"limit": 0})
            assert "limit" in text


class TestGetJobTool:
    async def test_the_tool_returns_the_same_status_as_rest(self, tmp_path):
        async with job_harness(tmp_path, seeded=1) as h:
            structured = await h.structured("get_job", {"job_id": "job-0"})
            assert structured == await h.rest_json("get", "/v1/jobs/job-0")

    async def test_an_unknown_job_is_a_tool_error_naming_the_id(self, tmp_path):
        async with job_harness(tmp_path) as h:
            text = await h.error_text("get_job", {"job_id": "job-none"})
            assert "job-none" in text


class TestControlJobTool:
    async def test_cancel_answers_the_same_status_as_rest(self, tmp_path):
        async with job_harness(tmp_path, seeded=1) as h:
            structured = await h.structured("control_job", {"job_id": "job-0", "action": "cancel"})
            assert structured["status"] == "cancelled"
            assert structured == await h.rest_json("get", "/v1/jobs/job-0")

    async def test_a_conflict_is_a_tool_error(self, tmp_path):
        async with job_harness(tmp_path, seeded=1, running=True) as h:
            await h.client.post(
                "/v1/jobs/job-0/control",
                json={"action": "pause"},
                headers={"X-API-Key": h.secret},
            )
            text = await h.error_text("control_job", {"job_id": "job-0", "action": "pause"})
            assert "pause" in text


class TestDeleteJobTool:
    async def test_the_tool_deletes_the_job_and_reports_it(self, tmp_path):
        async with job_harness(tmp_path, seeded=1) as h:
            structured = await h.structured("delete_job", {"job_id": "job-0"})

            assert structured == {"deleted": True, "job_id": "job-0"}
            rest = await h.client.get("/v1/jobs/job-0", headers={"X-API-Key": h.secret})
            assert rest.status_code == 404

    async def test_an_unknown_job_is_a_tool_error_and_deletes_nothing(self, tmp_path):
        async with job_harness(tmp_path) as h:
            text = await h.error_text("delete_job", {"job_id": "job-none"})
            assert "job-none" in text


class TestJobToolCallerScoping:
    async def test_another_callers_job_is_invisible_to_every_job_tool(self, tmp_path):
        async with job_harness(tmp_path, seeded=1) as h:
            other_secret = await issue_key(h.client)
            other = JobHarness(client=h.client, job_store=h.job_store, secret=other_secret)

            listed = await other.structured("list_jobs", {})
            get_text = await other.error_text("get_job", {"job_id": "job-0"})
            delete_text = await other.error_text("delete_job", {"job_id": "job-0"})

            assert listed == {"jobs": [], "total": 0}
            assert "job-0" in get_text
            assert "job-0" in delete_text


class TestJobToolDiscovery:
    def test_agent_hints_mention_the_job_tools(self):
        from crawlutron.discovery.agent_hints import agent_hints_payload

        guidance = " ".join(agent_hints_payload()["guidance"])
        assert "list_jobs" in guidance
        assert "delete_job" in guidance
