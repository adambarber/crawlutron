"""B4: the /v1/render 400 envelope must carry a stable error ``code``.

The handler raised ``http_error(400, str(error), str(error))``, so the
wire-visible ``code`` field was whatever the ValueError text happened to be
("batch_size_exceeded", "invalid_options", ...) while every other site in the
service emits the stable ``"validation_error"``. Clients key error handling on
``code``; a free-text code is an untracked, unenumerable contract.
"""

import pytest

from app.api.render_endpoint import render
from app.api_models import RenderRequest
from app.settings import CrawlerSettings


@pytest.fixture()
def _pool_up():
    """The pool-down guard answers 503 before validation runs; own the premise
    so the test exercises the ValueError branch, not startup ordering."""
    from app.crawl.strategies.browser_pool import set_browser_pool

    previous = set_browser_pool(object())
    yield
    set_browser_pool(previous)


async def test_render_value_error_reports_validation_error_code(monkeypatch, _pool_up):
    from dataclasses import replace

    settings = replace(CrawlerSettings.from_mapping(), max_batch_size=1)

    payload = RenderRequest(urls=["http://example.com/", "http://example.com/2"])
    with pytest.raises(Exception) as exc_info:
        await render(payload, settings)

    detail = exc_info.value.detail
    assert detail["code"] == "validation_error", detail
    # The ValueError text stays as the human-readable message, not the code.
    assert detail["message"] == "batch_size_exceeded"
