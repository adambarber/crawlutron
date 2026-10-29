"""#244/#205: /v1/render must honor options.respect_robots.

A Disallowed target used to render anyway; it is now robots_disallowed before the
browser worker touches it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import crawlutron.api.render as render_mod
from tests._fetcher_factories import routed_fetcher
from tests._render_producer import render_stub_request

if TYPE_CHECKING:
    import pytest

    from crawlutron.models.crawl_result import EngineCrawlResult
    from crawlutron.models.result_record import RenderEnvelope


def _disallow_fetcher() -> object:
    """A fetcher that disallows /secret and serves the home page."""
    return routed_fetcher(
        {"/robots.txt": (200, "User-agent: *\nDisallow: /secret\n", "text/plain")},
        default=(200, b"<html><body>ok</body></html>", "text/html"),
    )


async def _render(monkeypatch: pytest.MonkeyPatch, *, url: str, respect_robots: bool) -> RenderEnvelope:
    monkeypatch.setattr(render_mod, "default_http_fetcher", _disallow_fetcher())
    return await render_stub_request({"url": url, "options": {"respect_robots": respect_robots}})


def _first_result(envelope: RenderEnvelope) -> EngineCrawlResult:
    """The one result the render stub returns for its single URL."""
    results = envelope["results"]
    assert isinstance(results, list) and results
    return results[0]


async def test_render_with_respect_robots_disallows_an_explicit_path(monkeypatch):
    result = _first_result(
        await _render(monkeypatch, url="http://render-robots.test/secret", respect_robots=True)
    )
    assert result.classification == "robots_disallowed"
    assert result.status == "skipped"
    assert result.error == "robots_txt_disallowed"


async def test_render_with_respect_robots_allows_an_allowable_path(monkeypatch):
    result = _first_result(await _render(monkeypatch, url="http://render-robots.test/", respect_robots=True))
    assert result.classification == "rendered"  # not robots_disallowed


async def test_render_with_respect_robots_false_is_not_robots_disallowed(monkeypatch):
    result = _first_result(
        await _render(monkeypatch, url="http://render-robots.test/secret", respect_robots=False)
    )
    assert result.classification != "robots_disallowed"
