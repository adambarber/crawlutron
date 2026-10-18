"""#244/#205: /v1/render must honor options.respect_robots.

A Disallowed target used to render anyway; it is now robots_disallowed before the
browser worker touches it.
"""

from __future__ import annotations

import app.api.render as render_mod
from tests._fetcher_factories import routed_fetcher
from tests._render_producer import render_stub_request


def _disallow_fetcher() -> routed_fetcher:
    """A fetcher that disallows /secret and serves the home page."""
    return routed_fetcher(
        {"/robots.txt": (200, "User-agent: *\nDisallow: /secret\n", "text/plain")},
        default=(200, b"<html><body>ok</body></html>", "text/html"),
    )


async def _render(monkeypatch, *, url: str, respect_robots: bool) -> dict:
    monkeypatch.setattr(render_mod, "default_http_fetcher", _disallow_fetcher())
    return await render_stub_request({"url": url, "options": {"respect_robots": respect_robots}})


async def test_render_with_respect_robots_disallows_an_explicit_path(monkeypatch):
    result = (await _render(monkeypatch, url="http://render-robots.test/secret", respect_robots=True))[
        "results"
    ][0]
    assert result.classification == "robots_disallowed"
    assert result.status == "skipped"
    assert result.error == "robots_txt_disallowed"


async def test_render_with_respect_robots_allows_an_allowable_path(monkeypatch):
    result = (await _render(monkeypatch, url="http://render-robots.test/", respect_robots=True))["results"][0]
    assert result.classification == "rendered"  # not robots_disallowed


async def test_render_with_respect_robots_false_is_not_robots_disallowed(monkeypatch):
    result = (await _render(monkeypatch, url="http://render-robots.test/secret", respect_robots=False))[
        "results"
    ][0]
    assert result.classification != "robots_disallowed"
