"""The ``factory`` fixture hands over the four constructions it promises."""

from fastapi import FastAPI

from app.storage.job_store import JobStore
from app.transport.cookie_jar import CookieJar


def test_factory_fixture_builds_the_promised_surface(factory):
    assert isinstance(factory.app(), FastAPI)
    assert isinstance(factory.db(), JobStore)
    assert isinstance(factory.cookie_jar(), CookieJar)


async def test_factory_client_serves_an_isolated_app(factory):
    async with factory.client() as (client, _store, _results, _keys):
        assert (await client.get("/v1/keys")).status_code == 401
