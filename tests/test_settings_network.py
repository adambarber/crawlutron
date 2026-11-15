"""The service binds loopback by default: unauthenticated by design, the default
bind is 127.0.0.1, and only an explicit ``CRAWLUTRON_HOST=0.0.0.0`` (as the compose
file sets, where the loopback port mapping is the external control) exposes it.
"""

from crawlutron.config.settings import CrawlerSettings


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


def test_webhook_secret_lives_in_settings_not_the_service():
    """CRAWLUTRON_WEBHOOK_SECRET is read in the settings home (repr=False), not
    via a raw os.environ.get in service/webhooks.py."""
    settings = CrawlerSettings.from_mapping({"CRAWLUTRON_WEBHOOK_SECRET": "s3cr3t-webhook-key"})
    assert settings.webhook_secret == "s3cr3t-webhook-key"
    assert "s3cr3t-webhook-key" not in repr(settings)


def test_webhook_secret_defaults_to_empty():
    assert CrawlerSettings.from_mapping({}).webhook_secret == ""


class TestRetentionSweepGraceKnob:
    """The grace period is an env-read knob with bounds at the admission edge,
    not a bare module literal. Out-of-range is refused with a named event.
    """

    def test_default_grace_is_the_stated_24h_retention_promise(self):
        assert CrawlerSettings.from_mapping({}).retention_sweep_grace_seconds == 86400

    def test_env_override_reads_grace_seconds(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_RETENTION_SWEEP_GRACE_SECONDS": "1800"})
        assert settings.retention_sweep_grace_seconds == 1800

    def test_zero_grace_is_allowed(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_RETENTION_SWEEP_GRACE_SECONDS": "0"})
        assert settings.retention_sweep_grace_seconds == 0

    def test_out_of_range_grace_refuses_not_clamps(self, caplog):
        settings = _refused_grace("99999999", caplog)
        # Refused: yields the default, not the ceiling.
        assert settings.retention_sweep_grace_seconds == 86400
        # The refusal is visible at startup with the named event.
        refused = [r for r in caplog.records if r.message == "operator_retention_grace_out_of_range"]
        assert refused, "a refused deployment value must be visible at startup"
        assert refused[0].env_value == "99999999"

    def test_boundary_hi_plus_one_refuses(self, caplog):
        settings = _refused_grace("31536001", caplog)
        assert settings.retention_sweep_grace_seconds == 86400
        refused = [r for r in caplog.records if r.message == "operator_retention_grace_out_of_range"]
        assert refused, "one second past the ceiling must be refused"


def _refused_grace(env_value: str, caplog) -> CrawlerSettings:
    """Read a grace value expected to be refused, returning the settings."""
    import logging

    with caplog.at_level(logging.WARNING, logger="crawlutron.config.settings"):
        return CrawlerSettings.from_mapping({"CRAWLUTRON_RETENTION_SWEEP_GRACE_SECONDS": env_value})
