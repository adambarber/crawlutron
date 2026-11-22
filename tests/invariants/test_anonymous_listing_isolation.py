"""Anonymous listings are refused at the cores, so both doors inherit it (#1070)."""

from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest
from httpx import ASGITransport, AsyncClient

from crawlutron.service.job_cores import list_jobs_core
from crawlutron.service.watch_cores import list_watches_core
from tests._job_harnesses import _make_app
from tests._job_store_doubles import LoopGuardJobStore, LoopGuardWatchStore
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier
from tests._watch_harnesses import watch_client


def _refusal_line(fn) -> int | None:
    """The source line of the anonymous refusal in ``fn``, or None when absent."""
    lines, start = inspect.getsourcelines(fn)
    source = "".join(lines)
    if "caller.authenticated" not in source or "raise" not in source:
        return None
    for offset, line in enumerate(lines):
        if "caller.authenticated" in line:
            return start + offset
    return None


def _offenders() -> list[str]:
    """Each listing core missing the anonymous refusal, named at file:line."""
    out: list[str] = []
    for fn in (list_jobs_core, list_watches_core):
        if _refusal_line(fn) is None:
            file = pathlib.Path(inspect.getfile(fn)).name
            line = inspect.getsourcelines(fn)[1]
            out.append(f"{file}:{line}")
    return out


def test_listing_doors_refuse_anonymous_callers():
    """Each listing core refuses unauthenticated callers before the store query.

    Names each offender at file:line; goes red when the core refusal is removed.
    """
    offenders = _offenders()
    assert not offenders, "listing core missing the anonymous refusal: " + "; ".join(offenders)


@pytest.mark.asyncio
async def test_anonymous_listing_is_refused_at_the_production_door(tmp_path):
    """Two anonymous callers each create a job; each listing is refused (401)."""

    stores = [
        LoopGuardJobStore(
            str(tmp_path / f"anon-jobs-{i}.db"), policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier()
        )
        for i in range(2)
    ]
    clients = [
        AsyncClient(transport=ASGITransport(app=_make_app(store)), base_url="http://test") for store in stores
    ]
    try:
        for client in clients:
            created = await client.post(
                "/v1/jobs",
                json={"root_url": "https://example.test/root", "policy": {"limits": {"max_depth": 1}}},
            )
            assert created.status_code == 200, created.text

        for client in clients:
            listed = await client.get("/v1/jobs")
            assert listed.status_code == 401, listed.text
    finally:
        for client in clients:
            await client.aclose()
        for store in stores:
            await asyncio.to_thread(store.close)


@pytest.mark.asyncio
async def test_anonymous_watch_listing_is_refused_at_the_production_door(tmp_path):
    """Two anonymous callers each register a watch; each listing is refused (401)."""

    stores = [LoopGuardWatchStore(str(tmp_path / f"anon-watches-{i}.db")) for i in range(2)]
    clients = [watch_client(store) for store in stores]
    try:
        for client in clients:
            created = await client.post(
                "/v1/watches",
                json={"url": "https://example.test/root", "interval_seconds": 3600},
            )
            assert created.status_code == 201, created.text

        for client in clients:
            listed = await client.get("/v1/watches")
            assert listed.status_code == 401, listed.text
    finally:
        for client in clients:
            await client.aclose()
        for store in stores:
            await asyncio.to_thread(store.close)


__all__ = ["test_listing_doors_refuse_anonymous_callers"]
