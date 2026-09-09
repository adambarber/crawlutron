"""H5: Jobs API honesty — nothing executes whole-site jobs.

The Jobs API persists state (create/list/get/control/delete over SQLite)
but no engine consumes the frontier, so any operation implying execution
must refuse with 501 and name the gap, and the OpenAPI surface must not
promise a running/resumable crawl. Read/delete of persisted rows remain
honest state inspection and keep working.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_job_store
from app.crawl.job_store import JobStore


@pytest.fixture()
def app_with_store(tmp_path):
    """App wired to a fresh SQLite job store (no engine attached)."""
    from fastapi.exceptions import RequestValidationError

    from app.api.exceptions import (
        http_exception_handler,
        validation_exception_handler,
    )
    from app.api.jobs_router import router as jobs_router

    store = JobStore(str(tmp_path / "jobs.db"))
    application = FastAPI()
    application.include_router(jobs_router)
    application.add_exception_handler(HTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.dependency_overrides[get_job_store] = lambda: store
    return application


@pytest.fixture()
async def client(app_with_store):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_store), base_url="http://testserver"
    ) as async_client:
        yield async_client


class TestJobsExecutionHonesty:
    async def test_create_job_refuses_with_501(self, client):
        resp = await client.post("/v1/jobs", json={"root_url": "http://example.com/"})
        assert resp.status_code == 501
        body = resp.json()
        assert body["code"] == "jobs_not_executed"

    async def test_pause_and_resume_refuse_with_501(self, client):
        resp = await client.post(
            "/v1/jobs/{job_id}/control".format(job_id="whatever"),
            json={"action": "pause"},
        )
        assert resp.status_code == 501

    async def test_control_rejects_unknown_action_before_501(self, client):
        resp = await client.post(
            "/v1/jobs/{job_id}/control".format(job_id="whatever"),
            json={"action": "detonate"},
        )
        assert resp.status_code == 422

    async def test_list_and_get_still_work(self, client, app_with_store):
        store = None
        for dep in app_with_store.dependency_overrides.values():
            store = dep()
        assert store is not None
        job = store.create("http://example.com/", {})
        listed = await client.get("/v1/jobs")
        assert listed.status_code == 200
        assert listed.json()["jobs"], "persisted row should be listed"
        got = await client.get(f"/v1/jobs/{job.job_id}")
        assert got.status_code == 200

    async def test_get_missing_job_still_404s(self, client):
        resp = await client.get("/v1/jobs/nope")
        assert resp.status_code == 404

    async def test_delete_still_works(self, client, app_with_store):
        store = None
        for dep in app_with_store.dependency_overrides.values():
            store = dep()
        job = store.create("http://example.com/", {})
        resp = await client.delete(f"/v1/jobs/{job.job_id}")
        assert resp.status_code == 200

    async def test_openapi_does_not_advertise_execution(self, app_with_store):
        schema = app_with_store.openapi()
        jobs_text = str(schema["paths"]["/v1/jobs"])
        jobs_text += str(schema["paths"].get("/v1/jobs/{job_id}", ""))
        jobs_text += str(schema["paths"].get("/v1/jobs/{job_id}/control", ""))
        for phrase in ("resumable whole-site", "pause/resume/cancel a running"):
            assert phrase not in jobs_text
