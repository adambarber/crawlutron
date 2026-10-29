"""#323: render robots admission must not serialize distinct origins.

A batch render touching many cold origins used to pay one serial robots fetch
per origin before any browser worker started; the crawl path has been
bounded-concurrent since #544/P10, and render shares that doctrine.
"""

import crawlutron.api.render as render_mod
from tests._fetch_scripted import concurrent_fetcher
from tests._render_producer import render_stub_request

_ORIGINS = 8


def _multi_origin_fetcher():
    """Distinct cold origins, each serving an allowing robots.txt, tracking inflight."""
    return concurrent_fetcher(
        lambda url, _headers: (200, "User-agent: *\n", "text/plain"),
    )


async def test_render_batch_robots_fetches_overlap(monkeypatch):

    from crawlutron.transport.robots import robots_cache

    robots_cache.clear()
    fetcher = _multi_origin_fetcher()
    monkeypatch.setattr(render_mod, "default_http_fetcher", fetcher)
    urls = [f"http://render-r{i}.test/page" for i in range(_ORIGINS)]
    envelope = await render_stub_request({"urls": urls, "options": {"respect_robots": True}})
    assert envelope["submitted_count"] == _ORIGINS
    # Serial admission would drive peak to 1: distinct origins must overlap.
    assert fetcher.peak() >= 2, f"render robots fetches were serial: peak={fetcher.peak()}"
