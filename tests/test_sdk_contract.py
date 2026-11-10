"""Contract tests: the SDK client against the real crawlutron ASGI app.

No browser is launched and no external network is reached: these endpoints are
read-only or pure (admission), so a fresh app per test is hermetic.
"""

from __future__ import annotations

import pathlib

import httpx
import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.main import create_app
from crawlutron_sdk import CrawlutronClient
from crawlutron_sdk.models import AdmitRequest


def _new_client(tmp_path: pathlib.Path) -> CrawlutronClient:
    settings = CrawlerSettings(cache_dir=str(tmp_path / "cache"))
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    return CrawlutronClient(transport=transport, base_url="http://test")


@pytest.fixture
async def app_client(tmp_path):
    client = _new_client(tmp_path)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_health(app_client: CrawlutronClient):
    health = await app_client.health()
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_capabilities(app_client: CrawlutronClient):
    caps = await app_client.capabilities()
    assert caps.service
    assert caps.version
    assert "HTTP_DECLARED_CRAWLER" in caps.supported_strategies
    assert caps.features.get("async_mode") is True
    assert caps.features.get("captcha_solving") is False


@pytest.mark.asyncio
async def test_admit(app_client: CrawlutronClient):
    verdict = await app_client.admit(AdmitRequest(url="https://example.com/"))
    assert verdict.admitted is True
    assert verdict.canonical_url is not None


@pytest.mark.asyncio
async def test_admit_refuses_userinfo(app_client: CrawlutronClient):
    verdict = await app_client.admit(AdmitRequest(url="http://user:pass@example.com/"))
    assert verdict.admitted is False
    assert verdict.classification == "userinfo_refused"


@pytest.mark.asyncio
async def test_agent_hints(app_client: CrawlutronClient):
    hints = await app_client.agent_hints()
    assert hints.service
    assert isinstance(hints.guidance, list)
    assert len(hints.guidance) > 0


@pytest.mark.asyncio
async def test_ready(app_client: CrawlutronClient):
    ready = await app_client.ready()
    assert ready.status in {"ready", "saturated"}
    assert isinstance(ready.active_requests, int)
    assert isinstance(ready.saturated, bool)


@pytest.mark.asyncio
async def test_client_context_manager_closes(tmp_path: pathlib.Path):
    client = _new_client(tmp_path)
    async with client:
        health = await client.health()
    assert health.status == "ok"
