"""The service binds loopback by default: unauthenticated by design, the default
bind is 127.0.0.1, and only an explicit ``CRAWLUTRON_HOST=0.0.0.0`` (as the compose
file sets, where the loopback port mapping is the external control) exposes it.
"""

from app.config.settings import CrawlerSettings


def test_default_bind_is_loopback():
    assert CrawlerSettings.from_mapping({}).host == "127.0.0.1"


def test_bind_override_from_env():
    settings = CrawlerSettings.from_mapping({"CRAWLUTRON_HOST": "0.0.0.0"})
    assert settings.host == "0.0.0.0"


def test_cache_ttl_knob_zero_is_off_not_forever():
    """The OFF convention (#394): CRAWLUTRON_CACHE_TTL_SECONDS=0 reads as 'no
    content cache', never 'every default-TTL entry is permanent'."""
    zero = CrawlerSettings.from_mapping({"CRAWLUTRON_CACHE_TTL_SECONDS": "0"})
    assert zero.cache_ttl_seconds == 0
    negative = CrawlerSettings.from_mapping({"CRAWLUTRON_CACHE_TTL_SECONDS": "-10"})
    assert negative.cache_ttl_seconds == 0
    huge = CrawlerSettings.from_mapping({"CRAWLUTRON_CACHE_TTL_SECONDS": "999999"})
    assert huge.cache_ttl_seconds == 86_400
