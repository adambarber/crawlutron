"""MCP job tools (create_job, list_jobs, get_job, control_job, delete_job) over the job store.

The tools mirror the /v1/jobs REST doors: the same caller scoping, the same store reads, and -- for the
reads and the control action -- the same JSON the REST door answers with.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from crawlutron.service import job_runner
from crawlutron_rest.api.crawl_router import router as crawl_router
from crawlutron_rest.api.jobs_router import router as jobs_router
from crawlutron_rest.api.keys_router import router as keys_router
from crawlutron_rest.api.mcp_router import router as mcp_router
from tests._api_harnesses import (
    assert_crawl_result_served,
    isolated_delete_job_client,
    seed_completed_crawl,
)
from tests._job_harnesses import ScriptedExecute, _execution_policy, post_api_job
from tests._policy_factories import canonical
from tests._quota_harness import service_client
from tests._result_builders import engine_result
from tests.mcp._tool_calls import KeyedToolCaller, issue_key

_DELETE_JOB_ROUTERS = {
    "crawl": crawl_router,
    "jobs": jobs_router,
    "keys": keys_router,
    "mcp": mcp_router,
}

_JOBS_KEYS_MCP_ROUTERS = {
    "jobs": jobs_router,
    "keys": keys_router,
    "mcp": mcp_router,
}

if TYPE_CHECKING:
    from httpx import AsyncClient

    from crawlutron.storage.caller_key_store import CallerKeyStore
    from crawlutron.storage.job_store import JobStore


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


@pytest.fixture(autouse=True)
def _fresh_runner():
    job_runner.reset_job_runner()
    yield
    job_runner.reset_job_runner()


@dataclass(frozen=True)
class JobHarness(KeyedToolCaller):
    """One keyed harness: the client, the store, and the caller's secret."""

    client: AsyncClient
    job_store: object
    secret: str


@contextlib.asynccontextmanager
async def job_harness(tmp_path: Path, *, seeded: int = 0, running: bool = False):
    """A keyed harness with ``seeded`` single-shot jobs owned by the caller."""
    from crawlutron.storage.job_store import JobStatus

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


@asynccontextmanager
async def created_over_mcp(tmp_path):
    """A harness with one scripted-execution job created via the create_job tool.

    Every creation test needs the runner shim and the same tool call; yield
    ``(harness, response)`` so each test reads back only what it asserts."""
    job_runner.install_execute_crawl(
        ScriptedExecute(results=[engine_result("http://example.com/", "rendered")])
    )
    async with job_harness(tmp_path) as h:
        yield (
            h,
            await h.structured(
                "create_job", {"root_url": "http://example.com/", "policy": _execution_policy(None)}
            ),
        )


async def other_caller(h: JobHarness) -> JobHarness:
    """A second keyed caller on the same harness client (the cross-visibility actor)."""
    return JobHarness(client=h.client, job_store=h.job_store, secret=await issue_key(h.client))


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


class TestCreateJobTool:
    async def test_the_tool_creates_a_job_the_rest_door_answers(self, tmp_path):
        async with created_over_mcp(tmp_path) as (h, structured):
            assert structured["status"] == "pending"
            assert structured["root_url"] == canonical("http://example.com/").raw
            rest_job = await h.rest_json("get", f"/v1/jobs/{structured['job_id']}")
            assert rest_job["job_id"] == structured["job_id"]

    async def test_the_response_is_the_shape_rest_creates(self, tmp_path):
        async with created_over_mcp(tmp_path) as (h, mcp_created):
            rest = await post_api_job(h.client, "http://example.com/")

            assert rest.status_code == 200, rest.text
            assert list(mcp_created) == list(rest.json())

    async def test_an_invalid_root_url_is_a_tool_error(self, tmp_path):
        async with job_harness(tmp_path) as h:
            text = await h.error_text("create_job", {"root_url": "not-a-url"})

            assert "url" in text.lower()


class TestCreateJobCallerScoping:
    async def test_a_job_created_over_mcp_is_invisible_to_another_caller(self, tmp_path):
        async with created_over_mcp(tmp_path) as (h, structured):
            other = await other_caller(h)

            listed = await other.structured("list_jobs", {})
            text = await other.error_text("get_job", {"job_id": structured["job_id"]})

            assert listed == {"jobs": [], "total": 0}
            assert structured["job_id"] in text


class TestJobToolCallerScoping:
    async def test_another_callers_job_is_invisible_to_every_job_tool(self, tmp_path):
        async with job_harness(tmp_path, seeded=1) as h:
            other = await other_caller(h)

            listed = await other.structured("list_jobs", {})
            get_text = await other.error_text("get_job", {"job_id": "job-0"})
            delete_text = await other.error_text("delete_job", {"job_id": "job-0"})

            assert listed == {"jobs": [], "total": 0}
            assert "job-0" in get_text
            assert "job-0" in delete_text


class TestJobToolDiscovery:
    def test_agent_hints_mention_the_job_tools(self):
        from crawlutron_rest.discovery.agent_hints import agent_hints_payload

        guidance = " ".join(agent_hints_payload()["guidance"])
        assert "list_jobs" in guidance
        assert "create_job" in guidance
        assert "delete_job" in guidance


class TestDeleteJobPurgesResultStoreMCP:
    """DELETE job via the MCP delete_job tool must purge the result-store row,
    so GET /v1/crawl/{id} stops serving a permanently removed job's content.
    """

    async def test_deleting_a_completed_job_purges_its_result_row(self, tmp_path):
        from tests.mcp._tool_calls import tool_structured

        async with isolated_delete_job_client(tmp_path, _DELETE_JOB_ROUTERS) as (
            client,
            store,
            results,
            keys,
        ):
            secret = await issue_key(client)
            record = keys.verify_key(secret)
            assert record is not None
            owner = record.key_id
            headers = {"X-API-Key": secret}
            request_id = "crawl-completed-mcp-773"
            await seed_completed_crawl(store, results, owner, request_id)
            await assert_crawl_result_served(client, request_id, headers)

            structured = await tool_structured(client, "delete_job", {"job_id": request_id}, key=secret)
            assert structured == {"deleted": True, "job_id": request_id}

            after = await client.get(f"/v1/crawl/{request_id}", headers=headers)
            assert after.status_code == 404, after.text

    async def test_deleting_a_nonexistent_job_is_a_tool_error(self, tmp_path):
        from tests._api_harnesses import isolated_client
        from tests.mcp._tool_calls import tool_error_text

        async with isolated_client(tmp_path, routers=_JOBS_KEYS_MCP_ROUTERS) as (
            client,
            store,
            results,
            keys,
        ):
            secret = await issue_key(client)

            text = await tool_error_text(
                client, "delete_job", {"job_id": "crawl-never-happened-mcp-773"}, key=secret
            )
            assert "crawl-never-happened-mcp-773" in text
