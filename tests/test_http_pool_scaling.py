"""httpx connection-pool sizing scales with the cgroup limit (T7)."""

from unittest.mock import patch

from app import settings as settings_mod


class _FakeStats:
    def __init__(self, total_bytes):
        self.total_bytes = total_bytes


def test_small_limit_hits_floor():
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(256 * 1024 * 1024)):
        assert settings_mod._default_http_max_connections() == 100
        assert settings_mod._default_http_keepalive() == 20


def test_3gib_limit_scales_up():
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(3 * 1024 * 1024 * 1024)):
        assert settings_mod._default_http_max_connections() == 139
        assert settings_mod._default_http_keepalive() == 48


def test_huge_limit_is_capped():
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(64 * 1024 * 1024 * 1024)):
        assert settings_mod._default_http_max_connections() == 512
        assert settings_mod._default_http_keepalive() == 128


def test_env_override_still_wins():
    # CrawlerSettings still honors an explicit env override over the derived default.
    with patch.dict("os.environ", {"BROWSER_CRAWLER_HTTP_MAX_CONNECTIONS": "200"}):
        s = settings_mod.CrawlerSettings.from_mapping()
        assert s.http_max_connections == 200
