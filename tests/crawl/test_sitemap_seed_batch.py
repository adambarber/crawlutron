"""T7: sitemap freshness seeding must run off the event loop in one batch.

Each freshness check reads and parses the candidate's envelope from disk, so
running them inline on the loop stalls every concurrent crawl while a big
sitemap is filtered. The whole batch is offloaded via ``asyncio.to_thread``.
"""

from _testkit import install_to_thread_spy, make_fetch

from app.crawl.content_cache import ContentCache
from app.crawl.engine import _stale_seeds
from app.crawl.strategies.http import _content_cache_key, _headers_for_strategy

_CRAWL_URL = "http://seed-batch.test/"


def _policy():
    return {
        "strategies": ["HTTP_DECLARED_CRAWLER"],
        "extraction": "trafilatura",
        "headers": {},
    }


def _cache_with_fresh(tmp_path, url, lastmod, policy):
    """Seed the content cache so ``url`` is fresh as of ``lastmod``.

    The key must match what sitemap seeding looks up, so the caller passes the
    exact policy the engine derives (unit tests use ``_policy()`` directly; the
    integration test derives it via ``_payload_policy``).
    """
    cache = ContentCache(str(tmp_path), clock=lambda: 1_700_000_000.0)
    strategy = policy.get("strategies") or ["HTTP_DECLARED_CRAWLER"]
    key = _content_cache_key(
        url,
        strategy[0],
        _headers_for_strategy(strategy[0], policy, url),
        policy.get("extraction"),
        policy.get("css_selector"),
        policy.get("xpath"),
    )
    cache.set(key, {"html": "<html>cached</html>"})
    return cache


class TestStaleSeedBatch:
    def test_batch_matches_per_url_verdicts(self, tmp_path):
        """The batch helper returns exactly the URLs whose cached copy is
        fresh (the same verdicts the per-URL helper produced before T7)."""
        fresh = "http://seed-batch.test/fresh"
        stale = "http://seed-batch.test/stale"
        cache = _cache_with_fresh(tmp_path, fresh, "2020-01-01T00:00:00Z", _policy())
        policy = _policy()

        stale_set = _stale_seeds(
            [(fresh, "2020-01-01T00:00:00Z"), (stale, "2025-01-01T00:00:00Z")],
            cache,
            policy,
        )
        assert stale_set == {fresh}

    def test_unreadable_or_missing_entries_fail_open(self, tmp_path):
        """A cache read error must never cause a seed to be skipped: an
        unreadable/expired entry is treated as NOT fresh so the URL is still
        crawled."""
        cache = _cache_with_fresh(tmp_path, "http://seed-batch.test/ok", "2020-01-01", _policy())
        policy = _policy()

        stale_set = _stale_seeds(
            [("http://seed-batch.test/never-cached", "2020-01-01"), (None, "2020-01-01")],
            cache,
            policy,
        )
        assert stale_set == set()


class TestSeedingIsOffLoop:
    async def test_freshness_filter_runs_in_a_worker_thread(self, monkeypatch, tmp_path):
        """The engine's sitemap seeding must offload the freshness batch via
        ``asyncio.to_thread`` rather than parsing envelopes on the loop."""
        from app.crawl.engine import crawl_request

        seen = install_to_thread_spy(monkeypatch)
        fresh_url = "http://seed-batch.test/fresh"
        payload = {
            "url": _CRAWL_URL,
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "discovery": {"sitemaps": True},
                "limits": {
                    "timeout_seconds": 10,
                    "max_retries": 0,
                    "max_discovered_links": 10,
                    "max_depth": 1,
                },
            },
        }

        from app.crawl.payload_helpers import _payload_policy
        from app.settings import CrawlerSettings

        derived = _payload_policy(payload, CrawlerSettings.from_mapping())
        cache = _cache_with_fresh(tmp_path, fresh_url, "2020-01-01T00:00:00Z", derived)
        fetched: list[str] = []

        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            fetched.append(url)
            if url.endswith("/robots.txt"):
                return make_fetch(
                    url=url,
                    final_url=url,
                    status=200,
                    ctype="text/plain",
                    body=b"User-agent: *\nAllow: /\n",
                )
            if "sitemap" in url:
                body = (
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{fresh_url}</loc><lastmod>2020-01-01T00:00:00Z</lastmod></url>"
                    "</urlset>"
                ).encode()
                return make_fetch(
                    url=url,
                    final_url=url,
                    status=200,
                    ctype="application/xml",
                    body=body,
                )
            return make_fetch(
                url=url,
                final_url=url,
                status=200,
                ctype="text/html",
                body=b"<html><body>page</body></html>",
            )

        payload = {
            "url": _CRAWL_URL,
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "discovery": {"sitemaps": True},
                "limits": {
                    "timeout_seconds": 10,
                    "max_retries": 0,
                    "max_discovered_links": 10,
                    "max_depth": 1,
                },
            },
        }
        await crawl_request(payload, http_fetcher=fetcher, content_cache=cache)

        assert "_stale_seeds" in seen, "freshness filtering was not offloaded off the loop"
        # The fresh seed was skipped (#3): only robots.txt, the sitemap and the
        # crawled origin were fetched -- never the cached page itself.
        assert fresh_url not in fetched
