"""Stateless retry identity on the spend-heavy POST doors (#1152)."""

from __future__ import annotations

from pydantic import BaseModel

from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.service.request_identity import derive_request_id
from crawlutron_rest.api.caller_auth import get_caller_identity
from tests._app_factory import service_app
from tests._assertions import accepted_crawl
from tests._async_harnesses import crawl_api as _crawl_api
from tests._fetcher_factories import make_crawl_payload

_HEADER = "Idempotency-Key"

crawl_api = _crawl_api


async def _post_crawl(client, payload, token=None, *, header_name=_HEADER):
    """POST a crawl with an optional request-token header."""
    headers = None if token is None else {header_name: token}
    return await client.post("/v1/crawl", json=payload, headers=headers)


class TestCrawlRetryIdentity:
    """POST /v1/crawl replays the original job for a repeated token + payload."""

    async def test_same_token_and_payload_replays_original_202(self, crawl_api):
        store = service_app().state.job_store
        before = await store.acount_jobs()
        payload = crawl_api.payload(async_mode=True)

        first = await _post_crawl(crawl_api.client, payload, "crawl-retry-one")
        second = await _post_crawl(crawl_api.client, payload, "crawl-retry-one")

        assert first.status_code == second.status_code == 202
        original, retry = first.json(), second.json()
        assert original["status"] == retry["status"] == "accepted"
        assert original["request_id"] == retry["request_id"]
        assert original["status_url"] == retry["status_url"] == f"/v1/crawl/{original['request_id']}"
        assert await store.aexists(original["request_id"])
        assert await store.acount_jobs() == before + 1, "a retry must not persist a second crawl"

    async def test_same_token_different_payload_creates_distinct_jobs(self, crawl_api):
        first = await _post_crawl(crawl_api.client, crawl_api.payload(async_mode=True), "crawl-retry-two")
        other = make_crawl_payload(f"{crawl_api.base}/different", async_mode=True)
        second = await _post_crawl(crawl_api.client, other, "crawl-retry-two")

        assert first.status_code == second.status_code == 202
        assert first.json()["request_id"] != second.json()["request_id"]

    async def test_no_token_keeps_fresh_request_ids(self, crawl_api):
        first = await accepted_crawl(crawl_api.client, crawl_api.payload(async_mode=True))
        second = await accepted_crawl(crawl_api.client, crawl_api.payload(async_mode=True))
        assert first["request_id"] != second["request_id"]

    async def test_empty_token_is_untokened_behavior(self, crawl_api):
        first = await _post_crawl(crawl_api.client, crawl_api.payload(async_mode=True), "")
        second = await _post_crawl(crawl_api.client, crawl_api.payload(async_mode=True), "")
        assert first.status_code == second.status_code == 202
        assert first.json()["request_id"] != second.json()["request_id"]

    async def test_oversized_token_is_refused(self, crawl_api):
        response = await _post_crawl(crawl_api.client, crawl_api.payload(async_mode=True), "t" * 257)
        assert response.status_code == 422, response.text

    async def test_unicode_token_is_stable(self, crawl_api):
        payload = crawl_api.payload(async_mode=True)
        token = "resume-%C3%A9-%E4%BB%A4%E7%89%8C"
        first = await _post_crawl(crawl_api.client, payload, token)
        second = await _post_crawl(crawl_api.client, payload, token)
        assert first.json()["request_id"] == second.json()["request_id"]

    async def test_token_value_is_case_sensitive(self, crawl_api):
        payload = crawl_api.payload(async_mode=True)
        first = await _post_crawl(crawl_api.client, payload, "Token-Value")
        second = await _post_crawl(crawl_api.client, payload, "token-value")
        assert first.json()["request_id"] != second.json()["request_id"]

    async def test_header_name_is_case_insensitive(self, crawl_api):
        payload = crawl_api.payload(async_mode=True)
        first = await _post_crawl(crawl_api.client, payload, "case-header")
        second = await _post_crawl(crawl_api.client, payload, "case-header", header_name="idempotency-key")
        assert first.json()["request_id"] == second.json()["request_id"]

    async def test_keyed_and_anonymous_callers_do_not_collide(self, crawl_api, monkeypatch):
        payload = crawl_api.payload(async_mode=True)
        anonymous = await _post_crawl(crawl_api.client, payload, "shared-token")

        app = service_app()
        previous = app.dependency_overrides.get(get_caller_identity)
        app.dependency_overrides[get_caller_identity] = lambda: CallerIdentity(
            caller_id="key_a", authenticated=True
        )
        try:
            keyed = await _post_crawl(crawl_api.client, payload, "shared-token")
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_caller_identity, None)
            else:
                app.dependency_overrides[get_caller_identity] = previous

        assert anonymous.status_code == keyed.status_code == 202
        assert anonymous.json()["request_id"] != keyed.json()["request_id"]


class TestJobRetryIdentity:
    """POST /v1/jobs replays the original job for a repeated token + payload."""

    async def _post_job(self, client, payload, token=None):
        headers = None if token is None else {_HEADER: token}
        return await client.post("/v1/jobs", json=payload, headers=headers)

    async def test_same_token_and_payload_replays_original_job(self, crawl_api):
        payload = {"root_url": f"{crawl_api.base}/"}
        first = await self._post_job(crawl_api.client, payload, "job-retry-one")
        second = await self._post_job(crawl_api.client, payload, "job-retry-one")
        assert first.status_code == second.status_code == 200, second.text
        assert first.json()["job_id"] == second.json()["job_id"]

    async def test_same_token_different_payload_creates_distinct_jobs(self, crawl_api):
        first = await self._post_job(crawl_api.client, {"root_url": f"{crawl_api.base}/a"}, "job-retry-two")
        second = await self._post_job(crawl_api.client, {"root_url": f"{crawl_api.base}/b"}, "job-retry-two")
        assert first.status_code == second.status_code == 200, second.text
        assert first.json()["job_id"] != second.json()["job_id"]

    async def test_no_token_keeps_fresh_job_ids(self, crawl_api):
        payload = {"root_url": f"{crawl_api.base}/"}
        first = await self._post_job(crawl_api.client, payload)
        second = await self._post_job(crawl_api.client, payload)
        assert first.json()["job_id"] != second.json()["job_id"]


def test_openapi_advertises_the_request_token_header():
    spec = service_app().openapi()
    for path in ("/v1/crawl", "/v1/jobs"):
        params = spec["paths"][path]["post"].get("parameters", [])
        assert any(p["name"] == _HEADER and p["in"] == "header" for p in params), path


def test_caller_identity_is_bound_into_the_derived_id():
    payload = {"url": "https://example.com/"}
    ids = {
        derive_request_id("t", CallerIdentity(), payload, "secret"),
        derive_request_id("t", CallerIdentity(caller_id="key_a", authenticated=True), payload, "secret"),
        derive_request_id("t", CallerIdentity(caller_id="key_b", authenticated=True), payload, "secret"),
    }
    assert len(ids) == 3


def test_unicode_token_is_hashed_as_utf8():
    payload = {"url": "https://example.com/"}
    caller = CallerIdentity()
    assert derive_request_id("résumé-令牌", caller, payload, "secret") == derive_request_id(
        "résumé-令牌", caller, payload, "secret"
    )
    assert derive_request_id("résumé-令牌", caller, payload, "secret") != derive_request_id(
        "resume-", caller, payload, "secret"
    )


def test_fingerprint_bytes_survive_the_json_default_door():
    """Pin the HMAC bytes so a canonicalization drift reds (#1152)."""

    class Payload(BaseModel):
        url: str
        n: int

    mapping = {"url": "https://example.com/", "n": 3, "flag": True, "none": None}
    assert derive_request_id("t", CallerIdentity(), mapping, "secret") == (
        "crawl_ee30ce2be35d905c24003e93657bdad7"
    )
    assert (
        derive_request_id("t", CallerIdentity(caller_id="key_a", authenticated=True), mapping, "secret")
        == "crawl_c9190f45dcde05f921e399a4ea72ca66"
    )
    assert derive_request_id("t", CallerIdentity(), Payload(url="https://example.com/", n=3), "secret") == (
        "crawl_72ccf2db0819dcffaae129c11f6e4e53"
    )
