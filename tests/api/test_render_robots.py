"""#244/#205: /v1/render must honor options.respect_robots.

A Disallowed target used to render anyway; it is now robots_disallowed before the
browser worker touches it. #620 pins the WHY: refused vs unreachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import crawlutron.service.render as render_mod
from tests._fetch_doubles import FetcherDouble
from tests._render_producer import render_stub_request

if TYPE_CHECKING:
    import pytest

    from crawlutron.models.crawl_result import CrawlResult
    from crawlutron.models.result_record import RenderEnvelope


def _disallow_fetcher() -> object:
    """A fetcher that disallows /secret and serves the home page."""
    _robots = (200, "User-agent: *\nDisallow: /secret\n", "text/plain")
    _home = (200, b"<html><body>ok</body></html>", "text/html")
    return FetcherDouble(lambda url, _sent_headers, *_: _robots if "/robots.txt" in url.raw else _home)


async def _render(monkeypatch: pytest.MonkeyPatch, *, url: str, respect_robots: bool) -> RenderEnvelope:
    monkeypatch.setattr(render_mod, "default_http_fetcher", _disallow_fetcher())
    return await render_stub_request({"url": url, "options": {"respect_robots": respect_robots}})


def _first_result(envelope: RenderEnvelope) -> CrawlResult:
    """The one result the render stub returns for its single URL."""
    results = envelope["results"]
    assert isinstance(results, list) and results
    return results[0]


def _robots_evaluation(result: CrawlResult) -> object:
    """The ``metadata["robots"]["evaluation"]`` value, narrowed once per assert site."""
    meta = result.metadata
    robots = meta.get("robots")
    assert isinstance(robots, dict)
    return robots.get("evaluation")


def _status_fetcher(status: int) -> object:
    """A fetcher answering the robots fetch with ``status``, the page with 200."""
    _robots = (status, b"", "text/plain")
    _home = (200, b"<html></html>", "text/html")
    return FetcherDouble(lambda url, _sent_headers, *_: _robots if "/robots.txt" in url.raw else _home)


async def _render_raw(monkeypatch: pytest.MonkeyPatch, *, url: str, fetcher: object) -> RenderEnvelope:
    monkeypatch.setattr(render_mod, "default_http_fetcher", fetcher)
    return await render_stub_request({"url": url, "options": {"respect_robots": True}})


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


async def test_render_robots_403_refusal_says_refused_not_unreachable(monkeypatch):
    """#620: an explicit 401/403/429 denial carries robots_txt_refused through
    the render door -- a refusal is load-bearing, never read as an outage."""
    result = _first_result(
        await _render_raw(
            monkeypatch, url="http://render-robots-deny.test/secret", fetcher=_status_fetcher(403)
        )
    )
    assert result.classification == "robots_disallowed"
    assert result.error == "robots_txt_refused"
    assert _robots_evaluation(result) == "refused"


async def test_render_robots_loader_failure_fails_closed_as_refused(monkeypatch):
    """#620: an unresolved robots load (loader raises) fails closed as refused --
    never continue past an unresolved denial, and name it refused, not an outage."""
    from crawlutron.transport import robots as robots_transport

    async def raising_loader(*_args, **_kwargs):
        raise OSError("robots fetch exploded")

    monkeypatch.setattr(robots_transport, "load_robots_rules", raising_loader)
    result = _first_result(
        await _render_raw(
            monkeypatch, url="http://render-robots-boom.test/secret", fetcher=_status_fetcher(200)
        )
    )
    assert result.classification == "robots_disallowed"
    assert result.error == "robots_txt_refused"
    assert _robots_evaluation(result) == "refused"


async def test_render_robots_5xx_says_unreachable(monkeypatch):
    """#620: a 5xx robots answer is an outage, not a denial -- unreachable."""
    result = _first_result(
        await _render_raw(
            monkeypatch, url="http://render-robots-5xx.test/secret", fetcher=_status_fetcher(503)
        )
    )
    assert result.classification == "robots_disallowed"
    assert result.error == "robots_txt_unreachable"
    assert _robots_evaluation(result) == "unreachable"
