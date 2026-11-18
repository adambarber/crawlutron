"""Anonymous-bucket skip requires a verified body, not mere key presence (#1124).
A live key id with a wrong body still consumes the bucket; only a verified body skips.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from crawlutron.runtime import process_state
from crawlutron_rest.api.throttle import ANON_MAX_REQUESTS
from tests._async_harnesses import issue_key
from tests.factories.client import isolation_client

_NONE_EXISTENT_REQUEST = "0" * 64


def _wrong_body(secret: str, body: str) -> str:
    key_id = secret.split(".", 1)[0][3:]
    return f"ck_{key_id[len('key_') :]}.{body}"


@asynccontextmanager
async def _issued_client(tmp_path):
    """An isolation client with one issued key, yielded as ``(client, secret)``."""
    async with isolation_client(tmp_path) as (client, _store, _results, keys):
        secret = await issue_key(client)
        yield client, secret


async def _health(client, headers: dict[str, str]):
    return await client.get("/health", headers=headers)


async def _crawl_missing(client, headers: dict[str, str]):
    return await client.get(f"/v1/crawl/{_NONE_EXISTENT_REQUEST}", headers=headers)


async def _flood(client, headers: dict[str, str], count: int) -> list[int]:
    statuses = []
    for _ in range(count):
        resp = await _health(client, headers)
        statuses.append(resp.status_code)
    return statuses


async def _burns_bucket(client, headers: dict[str, str]):
    """Reset the meter, flood up to the boundary, and return the next response,
    which must be an anonymous 429 (presence alone never skips the bucket)."""
    process_state.reset_all_process_state()
    statuses = await _flood(client, headers, ANON_MAX_REQUESTS)
    assert all(s != 429 for s in statuses), statuses
    return await _health(client, headers)


async def _cache_proof(client, secret: str) -> None:
    """Reset the meter and make one cold verified request so the proof is cached,
    burning one anonymous unit."""
    good = {"X-API-Key": secret}
    process_state.reset_all_process_state()
    cold = await _crawl_missing(client, good)
    assert cold.status_code == 404, cold.status_code


@pytest.mark.parametrize("body", ["tampered-body", "trunc", "another-keys-body"])
async def test_wrong_body_still_burns_anonymous_bucket(tmp_path, body):
    """Known key id + wrong body consumes the anonymous bucket and 429s at the
    boundary; presence alone never skips (kills the presence-only mutant)."""
    async with _issued_client(tmp_path) as (client, secret):
        headers = {"X-API-Key": _wrong_body(secret, body)}
        refused = await _burns_bucket(client, headers)
        assert refused.status_code == 429, refused.status_code
        assert refused.json()["code"] == "anonymous_throttled"


async def test_wrong_body_via_bearer_still_burns_bucket(tmp_path):
    """Same wrong-body bypass attempt over Authorization: Bearer still meters."""
    async with _issued_client(tmp_path) as (client, secret):
        headers = {"Authorization": f"Bearer {_wrong_body(secret, 'tampered-body')}"}
        refused = await _burns_bucket(client, headers)
        assert refused.status_code == 429, refused.status_code


async def test_verified_body_then_wrong_body_burns_bucket(tmp_path):
    """Cold verified request caches the proof (burns one anonymous unit), then a
    wrong-body flood consumes ANON_MAX_REQUESTS - 1 more before 429."""
    async with _issued_client(tmp_path) as (client, secret):
        bad = {"X-API-Key": _wrong_body(secret, "wrong-body")}
        await _cache_proof(client, secret)
        statuses = await _flood(client, bad, ANON_MAX_REQUESTS - 1)
        assert all(s != 429 for s in statuses), statuses
        refused = await _health(client, bad)
        assert refused.status_code == 429, refused.status_code


async def test_verified_body_skips_anonymous_bucket(tmp_path):
    """#986 pin: once the body is verified and the proof cached, the bucket is
    skipped entirely (MAX+1 all pass, none 429)."""
    async with _issued_client(tmp_path) as (client, secret):
        await _cache_proof(client, secret)
        for _ in range(ANON_MAX_REQUESTS + 1):
            resp = await _health(client, {"X-API-Key": secret})
            assert resp.status_code != 429, resp.status_code


@pytest.mark.parametrize(
    "secret",
    [
        "garbage",
        "ck_0000000000000000000000000000000000000000000000000000000000000000.",
        "ck_0000000000000000000000000000000000000000000000000000000000000000.a.b",
        "ck_ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff.anybody",
    ],
)
async def test_parser_boundary_consumes_bucket(tmp_path, secret):
    """Garbage, empty body, double-dot, and unknown id all parse-fail or miss,
    so each consumes the anonymous bucket and 429s at the boundary."""
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        headers = {"X-API-Key": secret}
        refused = await _burns_bucket(client, headers)
        assert refused.status_code == 429, refused.status_code
