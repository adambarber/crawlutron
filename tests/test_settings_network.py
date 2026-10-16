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
