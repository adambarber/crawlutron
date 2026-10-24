"""#205/#320: /v1/map and /v1/render enforce page quota (429), wrong key (401).

The concurrency rework's code-review gate demanded committed router-level quota tests
through the public HTTP surface (the prior cycle had only a live probe)."""

from __future__ import annotations

import contextlib
from pathlib import Path

from app.api.identity_router import router as identity_router
from app.api.keys_router import router as keys_router
from tests._async_harnesses import expect_quota_refused, issue_key
from tests._quota_harness import service_client


@contextlib.asynccontextmanager
async def _map_render_client(tmp_path: Path):
    """Isolated app serving map, render, and keys routers over one register."""
    async with service_client(
        tmp_path,
        routers={"doors": identity_router, "keys": keys_router},
    ) as (client, job_store, keys):
        yield client


def _map_body(*, max_pages: int) -> dict[str, object]:
    return {"url": "http://example.com/", "policy": {"limits": {"max_pages": max_pages}}}


def _render_body() -> dict[str, object]:
    return {"urls": ["http://example.com/1", "http://example.com/2", "http://example.com/3"]}


async def test_map_rejects_over_bound_page_quota(tmp_path):
    """A map request over a keyed caller's page bound is 429 through the public router."""
    async with _map_render_client(tmp_path) as client:
        secret = await issue_key(client, max_pages=1)
        response = await client.post(
            "/v1/map",
            headers={"X-API-Key": secret},
            json=_map_body(max_pages=50),
        )
        expect_quota_refused(response)


async def test_map_wrong_key_is_401(tmp_path):
    """A map request with an unknown key is a 401, not a benign crawl."""
    async with _map_render_client(tmp_path) as client:
        response = await client.post(
            "/v1/map",
            headers={"X-API-Key": "definitely-not-a-key"},
            json=_map_body(max_pages=5),
        )
        assert response.status_code == 401, response.text


async def test_render_rejects_over_bound_page_quota(tmp_path):
    """A render of more urls than the keyed caller's page bound is 429."""
    async with _map_render_client(tmp_path) as client:
        secret = await issue_key(client, max_pages=1)
        response = await client.post(
            "/v1/render",
            headers={"X-API-Key": secret},
            json=_render_body(),
        )
        expect_quota_refused(response)


async def test_render_wrong_key_is_401(tmp_path):
    """A render request with an unknown key is a 401."""
    async with _map_render_client(tmp_path) as client:
        response = await client.post(
            "/v1/render",
            headers={"X-API-Key": "definitely-not-a-key"},
            json=_render_body(),
        )
        assert response.status_code == 401, response.text
