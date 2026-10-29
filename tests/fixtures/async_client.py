"""The ``async_client`` fixture: an ASGI-transport HTTP client over the app."""

import httpx
import pytest

from tests._app_factory import service_app


@pytest.fixture
async def async_client():
    """Async HTTP client using ASGI transport for FastAPI app."""
    async with httpx.ASGITransport(app=service_app()) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
