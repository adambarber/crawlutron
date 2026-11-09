"""Caller authentication refusal matrix: the #25 401/429 security surface.

Regression pins for the auth paths that had zero coverage (#199).
"""

from __future__ import annotations

from starlette.requests import Request

from crawlutron.api.key_throttle import KeyVerifyThrottle
from crawlutron.config.settings import CrawlerSettings
from crawlutron.storage.caller_key_store import CallerKeyStore
from tests._async_harnesses import issue_key
from tests.factories.client import isolation_client, post_crawl, quota_client
from tests.factories.store import complete_job


def test_203_cleanup_pins():
    # #676 inverted pin 1: the credential funnel is ONE parsing path, so guards read it
    # through the public door presented_key() (#203's private-only posture predates it).
    import crawlutron.api.caller_auth as caller_auth

    assert "presented_key" in caller_auth.__all__

    # #203 pin 2: CallerIdentity.label is written but never read in app/.
    from crawlutron.models.caller_identity import CallerIdentity

    assert "label" not in CallerIdentity.model_fields


def _assert_401(response) -> None:
    """A 401 with the invalid_api_key code, as every auth-refusal reads it."""
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "invalid_api_key"


async def test_missing_key_on_keys_door_is_401(tmp_path):
    """#199: no credential on /v1/keys is a 401, unauthenticated, not anonymous."""
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        listed = await client.get("/v1/keys")
        _assert_401(listed)
        minted = await client.post("/v1/keys", json={"label": "agent-a"})
        _assert_401(minted)


async def test_wrong_key_on_keys_door_is_401(tmp_path):
    """#199: a presented credential that is not the admin token is a 401."""
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        listed = await client.get("/v1/keys", headers={"X-API-Key": "not-the-token"})
        _assert_401(listed)


async def test_revoked_key_is_401(tmp_path):
    """#199: a key revoked after issuance can no longer crawl as itself."""
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
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
    async with isolation_client(tmp_path) as (client, store, _results, _keys):
        complete_job(store, "crawl_owned", {"request_id": "crawl_owned"}, owner="key_a")
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
            state=SimpleNamespace(
                crawler_settings=CrawlerSettings(key_admin_token="test-admin-token"),
                caller_key_store=keys,
            )
        ),
    }
    with pytest.raises(HTTPException) as raised:
        from crawlutron.api.caller_auth import get_keys_caller

        await get_keys_caller(Request(scope))
    assert raised.value.status_code == 401


async def test_keyless_crawl_stays_anonymous(tmp_path):
    """#199: a keyless crawl is the anonymous caller and is not refused."""
    async with quota_client(tmp_path) as (client, _store, _results, _keys):
        response = await post_crawl(client, max_pages=1)
        assert response.status_code == 202, response.text


async def test_a_valid_caller_key_cannot_mint_keys(tmp_path):
    """#436: a tenant key is not the operator; the keys door refuses to mint.

    Today the door falls through to ordinary verification, so any live key
    mints a fresh unbounded identity -- the escalation this refuses.
    """
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        secret = await issue_key(client)

        minted = await client.post("/v1/keys", json={"label": "escalated"}, headers={"X-API-Key": secret})

        _assert_401(minted)


async def test_a_valid_caller_key_cannot_revoke_another_callers_key(tmp_path):
    """#436: revocation is operator-only; a tenant key leaves a tenant's key live."""
    async with isolation_client(tmp_path) as (client, _store, _results, keys):
        victim_secret = await issue_key(client)
        attacker_secret = await issue_key(client)
        victim_key = keys.verify_key(victim_secret)
        assert victim_key is not None

        refused = await client.delete(f"/v1/keys/{victim_key.key_id}", headers={"X-API-Key": attacker_secret})

        _assert_401(refused)
        assert keys.verify_key(victim_secret) is not None


async def test_a_valid_caller_key_cannot_list_keys(tmp_path):
    """#436: the inventory is operator-only; a tenant key reads no other tenant's rows."""
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        secret = await issue_key(client)

        listed = await client.get("/v1/keys", headers={"X-API-Key": secret})

        _assert_401(listed)


async def test_wrong_admin_token_guesses_trip_the_cooldown(tmp_path):
    """#659: failed admin-token guesses on /v1/keys meter through the shared
    KeyVerifyThrottle's dedicated 'admin' bucket -- guess-pacing, not CPU
    protection (compare_digest is cheap); after MAX_FAILURES misses the door
    refuses 429 before spending any comparison work."""
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        for i in range(KeyVerifyThrottle.MAX_FAILURES):
            response = await client.get("/v1/keys", headers={"X-API-Key": f"wrong-guess-{i}"})
            assert response.status_code == 401, response.text
        throttled = await client.get("/v1/keys", headers={"X-API-Key": "wrong-guess-999"})
        assert throttled.status_code == 429, throttled.text
        assert throttled.json()["detail"]["code"] == "key_verify_throttled"


async def test_correct_admin_token_still_admits_after_metering(tmp_path):
    """#659: the meter paces guesses, not the operator -- a correct token
    presented under the MAX-failure bar still mints normally."""
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        for i in range(KeyVerifyThrottle.MAX_FAILURES - 1):
            await client.get("/v1/keys", headers={"X-API-Key": f"wrong-{i}"})
        issued = await client.post(
            "/v1/keys", json={"label": "agent-a"}, headers={"X-API-Key": "test-admin-token"}
        )
        assert issued.status_code == 201, issued.text


async def test_keys_door_is_sealed_when_no_operator_token_is_configured(tmp_path, monkeypatch):
    """#436: fail closed -- with no token configured, even a live key cannot mint.

    The documented contract (settings, .env.example) says an empty token
    disables the HTTP key surface; the door must not fall through.
    """
    from tests._async_harnesses import serve
    from tests._quota_harness import stores
    from tests.factories.client import isolation_app

    monkeypatch.delenv("CRAWLUTRON_KEY_ADMIN_TOKEN", raising=False)
    store, results, keys = stores(tmp_path)
    app = isolation_app(store, results, keys)
    app.state.crawler_settings = CrawlerSettings.from_mapping({"CRAWLUTRON_KEY_ADMIN_TOKEN": ""})
    async with serve(app) as client:
        issued = keys.issue_key("tenant-a")

        minted = await client.post(
            "/v1/keys", json={"label": "escalated"}, headers={"X-API-Key": issued.secret}
        )

        _assert_401(minted)


def _request_with(headers: dict[str, str]) -> Request:
    """A real Request with only headers set; app state absent, so the fallback funnel runs."""
    return Request(scope={"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})


def test_operator_token_rotation_is_honored_not_memoized():
    """#666: the operator token has ONE home, the settings object.

    The old lru_cache'd env fallback kept authenticating the rotated-away
    token (and refusing the new one) for the process lifetime -- a rotation
    was not a rotation. The red: after re-setting the env, the old token still
    authenticated and the new one was refused.
    """
    from types import SimpleNamespace

    import crawlutron.api.caller_auth as caller_auth

    settings = SimpleNamespace(key_admin_token="tok-old")
    scope = {
        "type": "http",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(crawler_settings=settings)),
    }

    def request_with(token: str) -> Request:
        scope["headers"] = [(b"x-api-key", token.encode())]
        return Request(scope)

    assert caller_auth.is_operator(request_with("tok-old")) is True

    settings.key_admin_token = "tok-new"
    assert caller_auth.is_operator(request_with("tok-old")) is False
    assert caller_auth.is_operator(request_with("tok-new")) is True


def test_with_no_settings_home_the_door_is_sealed(monkeypatch):
    """#666: the token's ONE home is the settings object; with no settings on
    app.state the door is sealed (fail closed), whatever the env says -- an
    env-var fallback was the memoized second home that broke rotation."""

    import crawlutron.api.caller_auth as caller_auth

    monkeypatch.setenv("CRAWLUTRON_KEY_ADMIN_TOKEN", "tok-from-env")
    request = _request_with({"x-api-key": "tok-from-env"})
    assert caller_auth.is_operator(request) is False


def test_the_settings_home_is_read_not_rederived(monkeypatch):
    """#636 successor: the one home is the state's settings object, read per
    request with no env parse -- rotation lands on the next request, and the
    operator door never re-derives the whole settings object to answer.
    """
    from types import SimpleNamespace

    import crawlutron.api.caller_auth as caller_auth
    from crawlutron.config.settings import CrawlerSettings

    calls = []
    real_from_mapping = CrawlerSettings.from_mapping

    def counting_from_mapping(*args, **kwargs):
        calls.append(1)
        return real_from_mapping(*args, **kwargs)

    monkeypatch.setattr(CrawlerSettings, "from_mapping", staticmethod(counting_from_mapping))
    settings = SimpleNamespace(key_admin_token="tok-settings")
    scope = {
        "type": "http",
        "headers": [(b"x-api-key", b"tok-settings")],
        "app": SimpleNamespace(state=SimpleNamespace(crawler_settings=settings)),
    }
    assert caller_auth.is_operator(Request(scope)) is True
    assert caller_auth.is_operator(Request(scope)) is True
    assert calls == [], "the door re-derived settings to answer a request"
