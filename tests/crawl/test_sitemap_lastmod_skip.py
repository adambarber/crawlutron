"""#3: sitemap <lastmod> + content_cache freshness skip."""

from app.crawl.content_cache import ContentCache
from app.crawl.engine import _parse_lastmod, _seed_is_fresh
from app.crawl.strategies.http import _content_cache_key, _headers_for_strategy


def _policy(extra=None):
    p = {"strategies": ["HTTP"], "extraction": "trafilara", "headers": {}}
    if extra:
        p.update(extra)
    return p


def test_parse_lastmod_variants():
    assert _parse_lastmod("2024-01-15") is not None
    assert _parse_lastmod("2024-01-15T10:30:00Z") is not None
    assert _parse_lastmod("Mon, 15 Jan 2024 10:30:00 GMT") is not None
    assert _parse_lastmod("") is None
    assert _parse_lastmod(None) is None
    assert _parse_lastmod("not-a-date") is None


def test_seed_is_fresh(tmp_path):
    cache = ContentCache(str(tmp_path), clock=lambda: 1_700_000_000.0)
    url = "http://example.com/page"
    policy = _policy()
    key = _content_cache_key(
        url, "HTTP", _headers_for_strategy("HTTP", policy, url),
        policy.get("extraction"), policy.get("css_selector"), policy.get("xpath"),
    )
    cache.set(key, {"html": "<html>...</html>"})

    # sitemap older than our cache -> cached copy is fresh -> skip
    assert _seed_is_fresh(url, "2020-01-01T00:00:00Z", cache, policy) is True
    # sitemap newer than our cache -> stale -> fetch
    assert _seed_is_fresh(url, "2025-01-01T00:00:00Z", cache, policy) is False
    # no lastmod -> fetch
    assert _seed_is_fresh(url, None, cache, policy) is False
    # not cached at all -> fetch
    assert _seed_is_fresh("http://example.com/other", "2020-01-01T00:00:00Z", cache, policy) is False


def test_seed_is_fresh_requires_cache():
    assert _seed_is_fresh("http://example.com/x", "2020-01-01T00:00:00Z", None, _policy()) is False
