"""The ``fake_provider_http`` fixture: a scripted search-provider response."""

import pytest


@pytest.fixture
def fake_provider_http(monkeypatch):
    """Patch ``crawlutron.search.providers.provider_http`` with a scripted response.

    Returns a callable that installs ``(status, body)`` and optionally records
    the kwargs of every provider HTTP call into ``capture``.
    """

    def _patch(status: int = 200, body: str = "{}", capture: list[dict[str, object]] | None = None):
        async def _fake(method, url, **kwargs):
            if capture is not None:
                capture.append({"method": method, "url": url, **kwargs})
            return status, body

        monkeypatch.setattr("crawlutron.search.providers.provider_http", _fake)
        return _fake

    return _patch
