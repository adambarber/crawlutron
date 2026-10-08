"""The service binds loopback by default.

Unauthenticated by design, so the default bind must be 127.0.0.1; only an
explicit CRAWLUTRON_HOST=0.0.0.0 (as the compose file sets, where the
loopback ports mapping is the external control) exposes the port.
"""

from app.config.settings import CrawlerSettings


def test_default_bind_is_loopback():
    assert CrawlerSettings.from_mapping({}).host == "127.0.0.1"


def test_bind_override_from_env():
    settings = CrawlerSettings.from_mapping({"CRAWLUTRON_HOST": "0.0.0.0"})
    assert settings.host == "0.0.0.0"
