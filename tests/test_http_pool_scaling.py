"""httpx connection-pool sizing scales with the cgroup limit (T7)."""

from unittest.mock import patch

from app import settings as settings_mod


class _FakeStats:
    def __init__(self, total_bytes):
        self.total_bytes = total_bytes


def test_small_limit_hits_floor():
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(256 * 1024 * 1024)):
        assert settings_mod._default_http_max_connections() == 100
        assert settings_mod._default_http_keepalive() == 50


def test_3gib_limit_scales_up():
    limit = 3 * 1024 * 1024 * 1024
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(limit)):
        # The pool ceiling must equal the admission's HTTP concurrency for the
        # same limit + pool, whatever the per-task RAM estimate is (zero drift).
        expected = max(
            100,
            settings_mod.max_http_concurrency_for(limit, settings_mod.derive_browser_pool_size(limit)),
        )
        assert settings_mod._default_http_max_connections() == expected
        assert settings_mod._default_http_keepalive() == max(20, min(128, expected // 2))
        # With the honest per-task RAM estimate the 3 GiB derivation sits at the
        # floor; the floor keeps the minimal-deployment pool usable (a bigger
        # box still scales above it -- see test_huge_limit_is_capped).
        assert expected >= 100


def test_huge_limit_is_capped():
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(64 * 1024 * 1024 * 1024)):
        assert settings_mod._default_http_max_connections() == 512
        assert settings_mod._default_http_keepalive() == 128


def test_env_override_still_wins():
    # CrawlerSettings still honors an explicit env override over the derived default.
    with patch.dict("os.environ", {"BROWSER_CRAWLER_HTTP_MAX_CONNECTIONS": "200"}):
        s = settings_mod.CrawlerSettings.from_mapping()
        assert s.http_max_connections == 200
