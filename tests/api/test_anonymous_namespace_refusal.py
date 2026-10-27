"""#559: anonymous callers may not declare a cookie namespace -- refused at every door.

Anonymous is ONE bounded shared identity (M1 #206), so a declared namespace is
not a tenant boundary; the doors answer 422 ``anonymous_cookie_namespace``
instead of the #537-style silent downgrade. The walk-past scenario (two
anonymous callers, one declared namespace, a shared jar) lives here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest

from app.api.caller_auth import effective_cookie_namespace
from app.models.caller_identity import CallerIdentity
from app.storage.job_store import JobStore
from tests.factories.store import job_store as make_store


class _DoorEnvelope(TypedDict):
    """The refusal envelope the error handler writes (ErrorResponse shape)."""

    code: str
    message: str


def _door_envelope(response) -> _DoorEnvelope:
    """The refusal envelope: bare test apps wrap it in ``detail``, the real
    app factory unwraps it at the top level."""
    body = response.json()
    return body if "code" in body else body["detail"]


def _refusal_is_envelope(response) -> None:
    """One assertion for every door: the 422 carries the #559 code."""
    assert response.status_code == 422, response.text
    assert _door_envelope(response)["code"] == "anonymous_cookie_namespace"


#: One policy-carrying request body declaring a tenant namespace; only the
#: door path differs between the crawl, map, and jobs refusals.
_DECLARED_BODY = {
    "limits": {"max_pages": 1},
    "request_profile": {"cookie_namespace": "tenant-x"},
}


def test_anonymous_declaration_is_refused_at_the_crawl_door(tmp_path: Path):
    """#559: an anonymous /v1/crawl that declares request_profile.cookie_namespace
    is a 422 at the door -- not an honored claim (the F-1 residual) and not a
    silent downgrade (#537). A keyed caller declaring one stays 202 and pinned."""
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from app.api.dependencies import get_settings, get_shutdown_event
    from app.config.settings import CrawlerSettings
    from tests.factories.client import isolation_app, stores

    async def run():
        store, results, keys = stores(tmp_path)
        app = isolation_app(store, results, keys)
        app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
        app.dependency_overrides[get_shutdown_event] = asyncio.Event
        issued = keys.issue_key("agent-a")
        declared = {
            "url": "http://example.com/",
            "async_mode": True,
            "policy": {
                "limits": {"max_pages": 1},
                "request_profile": {"cookie_namespace": "tenant-x"},
            },
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            refused = await client.post("/v1/crawl", json=declared)
            assert refused.status_code == 422, refused.text
            envelope = _door_envelope(refused)
            assert envelope["code"] == "anonymous_cookie_namespace"
            assert "anonymous" in envelope["message"].lower()

            keyed = await client.post("/v1/crawl", headers={"X-API-Key": issued.secret}, json=declared)
            assert keyed.status_code == 202, keyed.text

        stored = store.load(keyed.json()["request_id"])
        assert stored is not None
        # #226: the keyed declaration is pinned to the key id, never the declared text.
        assert stored.policy.request_profile.get("cookie_namespace") == issued.key_id

    asyncio.run(run())


def test_anonymous_callers_cannot_share_a_declared_namespace_jar(tmp_path: Path):
    """#559: two anonymous callers, same declared namespace -- the declaration is refused,
    so the second never reads the jar the first planted under it. Red before the fix: the
    passthrough honored 'tenant-x' and the cookie crossed."""
    from app.storage.coordination_cookies import SharedCookieStore
    from app.transport.cookie_jar import CookieJar
    from tests._policy_factories import canonical

    target = canonical("http://example.com/")
    jar = CookieJar(SharedCookieStore(tmp_path / "shared-jar.db"))
    # Anonymous tenant A harvested a session under its declared namespace.
    jar.add_cookie_header(target, "session=tenant-a", namespace="tenant-x")
    # Anonymous tenant B declares the same namespace and must not read it: the
    # declaration resolves to the shared default (anonymous is ONE bounded identity).
    namespace = effective_cookie_namespace({"cookie_namespace": "tenant-x"}, CallerIdentity())
    assert namespace == ""
    assert jar.cookie_header_for(target, namespace=namespace) == ""


@pytest.mark.asyncio
async def test_anonymous_map_declaration_is_refused_at_the_door(tmp_path: Path):
    """#559: the /v1/map door refuses an anonymous cookie_namespace declaration."""
    from app.api.identity_router import router as identity_router
    from tests._quota_harness import service_client

    async with service_client(tmp_path, routers={"doors": identity_router}) as (client, _s, _k):
        refused = await client.post(
            "/v1/map",
            json={"url": "http://example.com/", "policy": dict(_DECLARED_BODY)},
        )
        _refusal_is_envelope(refused)


@pytest.mark.asyncio
async def test_anonymous_jobs_declaration_is_refused_at_the_door(tmp_path: Path):
    """#559: the /v1/jobs door refuses an anonymous declaration before any durable row."""
    from httpx import ASGITransport, AsyncClient

    from app.api.dependencies import get_settings
    from app.config.settings import CrawlerSettings
    from tests._job_harnesses import _make_app

    store: JobStore = make_store(tmp_path / "jobs.db")
    app = _make_app(store)
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            "/v1/jobs",
            json={"root_url": "http://example.com/", "policy": dict(_DECLARED_BODY)},
        )
    _refusal_is_envelope(refused)
    assert store.list_jobs() == [], "a refused declaration must not create a job"
