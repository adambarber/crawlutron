"""L5c: crawl/render requests take exactly one of url/urls.

Both set silently ignored url; neither failed downstream in _payload_urls.
Fail fast at the model boundary instead.
"""

import httpx
import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.api_models import CrawlRequest, RenderRequest
from tests._app_factory import service_app


@pytest.mark.parametrize("model", [CrawlRequest, RenderRequest])
def test_url_and_urls_together_rejected(model):
    with pytest.raises(ValidationError):
        model(url="http://example.com/", urls=["http://example.com/"])


@pytest.mark.parametrize("model", [CrawlRequest, RenderRequest])
def test_neither_url_nor_urls_rejected(model):
    with pytest.raises(ValidationError):
        model()
    with pytest.raises(ValidationError):
        model(urls=[])


@pytest.mark.parametrize("model", [CrawlRequest, RenderRequest])
def test_exactly_one_url_source_accepted(model):
    assert model(url="http://example.com/").url == "http://example.com/"
    assert model(urls=["http://example.com/"]).urls == ["http://example.com/"]


@pytest.mark.parametrize("model", [CrawlRequest, RenderRequest])
def test_empty_urls_list_with_url_rejected(model):
    """N4: urls=[] is not "absent" -- exclude_none dumps it, _payload_urls
    resolves it to zero targets, and url is silently ignored."""
    with pytest.raises(ValidationError):
        model(url="http://example.com/", urls=[])


class TestWebhookUrlValidation:
    async def test_main_invalid_webhook_url(self):
        transport = httpx.ASGITransport(app=service_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/crawl", json={"url": "http://example.com/", "webhook_url": "not_a_url"}
            )
            assert resp.status_code == 422
