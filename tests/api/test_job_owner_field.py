"""#407: the owner field's VALUE in the response body is a held contract.

The description says: empty for every anonymous caller, the key id on every
keyed one, and a foreign caller cannot see the row at all. Nothing pinned it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crawlutron.api.caller_auth import get_caller_identity
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.storage.job_store import JobStore
from tests._dependency_overrides import bind_base_overrides
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier
from tests._result_builders import GRAPH_ROOT


def _app(job_store: JobStore, caller: CallerIdentity) -> FastAPI:
    from crawlutron.api.jobs_router import router as jobs_router

    app = FastAPI()
    app.include_router(jobs_router)
    bind_base_overrides(app, job_store=job_store, shutdown_event=asyncio.Event())
    app.dependency_overrides[get_caller_identity] = lambda: caller
    return app


@contextlib.asynccontextmanager
async def _client(job_store: JobStore, caller: CallerIdentity) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_app(job_store, caller))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _create(client: AsyncClient) -> str:
    response = await client.post("/v1/jobs", json={"root_url": GRAPH_ROOT})
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _store(tmp_path: Path, name: str) -> JobStore:
    return JobStore(tmp_path / name, policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())


async def _reported_owner(tmp_path: Path, name: str, caller: CallerIdentity) -> tuple[str, str]:
    """Create a job as ``caller`` and return (GET owner, list owner)."""
    store = _store(tmp_path, name)
    async with _client(store, caller) as client:
        job_id = await _create(client)
        body = (await client.get(f"/v1/jobs/{job_id}")).json()
        listed = (await client.get("/v1/jobs")).json()["jobs"][0]
        return body["owner"], listed["owner"]


async def test_an_anonymous_job_reports_an_empty_owner(tmp_path: Path):
    assert await _reported_owner(tmp_path, "anon.db", CallerIdentity()) == ("", "")


async def test_a_keyed_job_reports_its_key_id_as_owner(tmp_path: Path):
    keyed = CallerIdentity(caller_id="key_a", authenticated=True)
    assert await _reported_owner(tmp_path, "keyed.db", keyed) == ("key_a", "key_a")


async def test_a_job_from_another_caller_is_not_visible_at_all(tmp_path: Path):
    store = _store(tmp_path, "foreign.db")
    async with _client(store, CallerIdentity(caller_id="key_a", authenticated=True)) as client:
        job_id = await _create(client)
    async with _client(store, CallerIdentity(caller_id="key_b", authenticated=True)) as client:
        assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 404
        assert (await client.get("/v1/jobs")).json()["jobs"] == []
