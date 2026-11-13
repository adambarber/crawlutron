"""DELETE /v1/jobs/{id} purges the CrawlResultStore row so GET /v1/crawl/{id}
stops serving a permanently removed job's content.
"""

from __future__ import annotations

from crawlutron_rest.api.crawl_router import router as crawl_router
from crawlutron_rest.api.jobs_router import router as jobs_router_mod
from crawlutron_rest.api.keys_router import router as keys_router
from tests._api_harnesses import (
    assert_crawl_result_served,
    assert_delete_purges_result,
    isolated_delete_job_client,
    issue_agent_key,
    seed_completed_crawl,
)

_DELETE_JOB_ROUTERS = {
    "crawl": crawl_router,
    "jobs": jobs_router_mod,
    "keys": keys_router,
}


class TestDeleteJobPurgesResultStore:
    """DELETE /v1/jobs/{id} must purge the job's CrawlResultStore row."""

    async def test_deleting_a_completed_job_purges_its_result_row(self, tmp_path):
        async with isolated_delete_job_client(tmp_path, _DELETE_JOB_ROUTERS) as (
            client,
            store,
            results,
            keys,
        ):
            secret, owner, headers = issue_agent_key(keys)
            request_id = "crawl-completed-773"
            await seed_completed_crawl(store, results, owner, request_id)
            await assert_crawl_result_served(client, request_id, headers)
            await assert_delete_purges_result(client, request_id, headers)

    async def test_deleting_a_running_job_tombstones_without_result_row(self, tmp_path):
        from crawlutron.storage.job_store import JobStatus
        from tests._api_harnesses import isolated_client

        async with isolated_client(tmp_path, routers=_DELETE_JOB_ROUTERS) as (
            client,
            store,
            results,
            keys,
        ):
            secret, owner, headers = issue_agent_key(keys)
            request_id = "crawl-running-773"

            store.create_single_shot(request_id, "http://example.com/", {}, owner=owner)
            await store.aset_status(request_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
            await assert_delete_purges_result(client, request_id, headers)
            assert await store.astatus_for_caller(request_id, owner) is None

    async def test_deleting_a_nonexistent_job_returns_404_unchanged(self, tmp_path):
        _JOBS_KEYS_ROUTERS = {
            "jobs": jobs_router_mod,
            "keys": keys_router,
        }
        async with isolated_delete_job_client(tmp_path, _JOBS_KEYS_ROUTERS) as (
            client,
            store,
            results,
            keys,
        ):
            secret, owner, headers = issue_agent_key(keys)

            response = await client.delete("/v1/jobs/crawl-never-happened-773", headers=headers)
            assert response.status_code == 404, response.text
            assert "crawl-never-happened-773" in response.text

    async def test_delete_job_core_without_result_store_is_fail_open(self, job_store, result_store):
        """result_store=None: the job is still tombstoned, no crash, and the
        result row is left untouched (fail-open mirrors the optional param)."""
        from crawlutron.models.caller_identity import CallerIdentity
        from crawlutron.models.job_id_request import JobIdRequest
        from crawlutron.service.job_cores import delete_job_core

        caller = CallerIdentity(caller_id="agent-a", authenticated=True)
        request_id = "fail-open-773"
        await seed_completed_crawl(job_store, result_store, "agent-a", request_id)

        response = await delete_job_core(
            JobIdRequest(job_id=request_id),
            job_store,
            None,
            None,
            caller,
            result_store=None,
        )
        assert response.deleted is True
        assert await job_store.astatus_for_caller(request_id, "agent-a") is None
        assert await result_store.aget_for_caller(request_id, "agent-a") is not None
