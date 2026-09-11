"""H7: sitemap-index fan-out must be bounded at enqueue time.

A hostile <sitemapindex> with tens of thousands of children must not spawn
one task per child. Child scheduling is capped by remaining max_urls need
and processed in bounded-concurrency chunks. Uses real_sleeps so task
overlap is genuine (the suite otherwise patches asyncio.sleep instant,
which would serialize the fan-out and mask the explosion).
"""

import asyncio

from _testkit import make_fetch

from app.crawl.fetch_helpers import _SITEMAP_CACHE, fetch_sitemap_urls


def _resp(body: str):
    return make_fetch(
        url="http://fanout.test/sitemap.xml",
        final_url="http://fanout.test/sitemap.xml",
        status=200,
        ctype="application/xml",
        body=body.encode("utf-8"),
    )


def _index_xml(n: int) -> str:
    children = "".join(f"<sitemap><loc>http://fanout.test/child{i}.xml</loc></sitemap>" for i in range(n))
    return (
        '<?xml version="1.0"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{children}</sitemapindex>"
    )


def _child_xml(i: int) -> str:
    return (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>http://fanout.test/p{i}</loc></url></urlset>"
    )


class TestSitemapFanoutBound:
    async def test_child_scheduling_capped_by_remaining_need(self, real_sleeps, monkeypatch):
        url = "http://fanout.test/sitemap.xml"
        _SITEMAP_CACHE.discard(url)
        index_body = _index_xml(200)
        in_flight = 0
        max_in_flight = 0
        child_fetches = 0
        tasks_created = 0
        real_create_task = asyncio.create_task

        def counting_create_task(coro, **kwargs):
            nonlocal tasks_created
            tasks_created += 1
            return real_create_task(coro, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", counting_create_task)

        async def fetcher(u, headers, timeout, max_content_length=50 * 1024 * 1024):
            nonlocal in_flight, max_in_flight, child_fetches
            if u != url:
                child_fetches += 1
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                try:
                    await asyncio.sleep(0.01)
                    idx = int(u.split("child")[1].split(".")[0])
                    return _resp(_child_xml(idx))
                finally:
                    in_flight -= 1
            return _resp(index_body)

        try:
            out = await fetch_sitemap_urls(fetcher, url, 10, max_urls=20)
        finally:
            _SITEMAP_CACHE.discard(url)
        assert len(out) == 20
        # Enqueue-time cap: never schedule more children than still needed.
        assert child_fetches <= 20
        assert tasks_created <= 20
        # Bounded concurrency: chunks, not one task per child.
        assert max_in_flight <= 16
