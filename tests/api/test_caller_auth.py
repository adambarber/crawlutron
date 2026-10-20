"""Caller authentication refusal matrix: the #25 401/429 security surface.

Regression pins for the auth paths that had zero coverage (#199).
"""

from __future__ import annotations

from app.storage.caller_key_store import CallerKeyStore
from tests.api.test_caller_isolation import (
    _complete_job,
    _isolation_client,
    _post_crawl,
    _quota_client,
)


def _assert_401(response) -> None:
    """A 401 with the invalid_api_key code, as every auth-refusal reads it."""
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "invalid_api_key"


async def test_missing_key_on_keys_door_is_401(tmp_path):
    """#199: no credential on /v1/keys is a 401, unauthenticated, not anonymous."""
    async with _isolation_client(tmp_path) as (client, _store, _results, _keys):
        listed = await client.get("/v1/keys")
        _assert_401(listed)
        minted = await client.post("/v1/keys", json={"label": "agent-a"})
        _assert_401(minted)


async def test_wrong_key_on_keys_door_is_401(tmp_path):
    """#199: a presented credential that is not the admin token is a 401."""
    async with _isolation_client(tmp_path) as (client, _store, _results, _keys):
        listed = await client.get("/v1/keys", headers={"X-API-Key": "not-the-token"})
        _assert_401(listed)


async def test_revoked_key_is_401(tmp_path):
    """#199: a key revoked after issuance can no longer crawl as itself."""
    async with _isolation_client(tmp_path) as (client, _store, _results, _keys):
        admin = {"X-API-Key": "test-admin-token"}
        issued = await client.post("/v1/keys", json={"label": "agent-a"}, headers=admin)
        assert issued.status_code == 201, issued.text
        key_id = issued.json()["key_id"]
        revoked = await client.delete(f"/v1/keys/{key_id}", headers=admin)
        assert revoked.status_code == 200, revoked.text
        crawl = await client.post(
            "/v1/crawl",
            headers={"X-API-Key": issued.json()["secret"]},
            json={"url": "http://example.com/", "async_mode": True},
        )
        _assert_401(crawl)


async def test_wrong_key_on_crawl_read_is_401(tmp_path):
    """#199: an authenticated caller with a wrong key is 401 before any read scope."""
    async with _isolation_client(tmp_path) as (client, store, _results, _keys):
        _complete_job(store, "crawl_owned", {"request_id": "crawl_owned"}, owner="key_a")
        response = await client.get("/v1/crawl/crawl_owned", headers={"X-API-Key": "definitely-wrong"})
        _assert_401(response)


async def test_non_ascii_key_on_keys_door_is_401(tmp_path):
    """A non-ASCII presented key is a 401, never a 500 (#199).

    ``compare_digest`` raises TypeError on non-ASCII str; rendered at the
    handler because httpx refuses to send a non-ASCII header value.
    """
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException
    from starlette.requests import Request

    keys = CallerKeyStore(tmp_path / "keys.db")
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/keys",
        "query_string": b"",
        # A real gateway decodes a latin-1 header byte to a non-ASCII str.
        "headers": [(b"x-api-key", "cafééé".encode("latin-1"))],
        "app": SimpleNamespace(
            state=SimpleNamespace(key_admin_token="test-admin-token", caller_key_store=keys)
        ),
    }
    with pytest.raises(HTTPException) as raised:
        from app.api.caller_auth import get_keys_caller

        await get_keys_caller(Request(scope))
    assert raised.value.status_code == 401


async def test_keyless_crawl_stays_anonymous(tmp_path):
    """#199: a keyless crawl is the anonymous caller and is not refused."""
    async with _quota_client(tmp_path) as (client, _store, _results, _keys):
        response = await _post_crawl(client, max_pages=1)
        assert response.status_code == 202, response.text
