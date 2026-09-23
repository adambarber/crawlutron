"""CrawlerSettings defaults tuned for faster parallel single-domain crawls (#7)."""

import logging

from app.settings import CrawlerSettings


def test_parallelism_defaults():
    s = CrawlerSettings.from_mapping()
    # #7: faster single-domain crawling without being reckless.
    assert s.per_domain_concurrency == 2
    assert s.per_domain_delay_ms == 500
    # Adaptive throttling capped so a slow server cannot serialize crawls to
    # a 30s-per-request crawl.
    assert s.adaptive_max_delay == 10.0


class TestFromMappingHardening:
    """H1: from_mapping must absorb garbage env values, never crash, and
    never fail a policy open on a typo'd boolean."""

    def test_garbage_numeric_values_fall_back_to_defaults(self, caplog):
        settings = CrawlerSettings.from_mapping(
            {
                "BROWSER_CRAWLER_GLOBAL_CONCURRENCY": "four",
                "BROWSER_CRAWLER_ADAPTIVE_MIN_DELAY": "not-a-float",
            }
        )
        assert not hasattr(settings, "host_port")
        assert settings.global_concurrency == 4
        assert settings.adaptive_min_delay == 0.5

    def test_typo_respect_robots_fails_closed_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.validation"):
            settings = CrawlerSettings.from_mapping({"BROWSER_CRAWLER_RESPECT_ROBOTS": "ture"})
        assert settings.respect_robots is True  # dataclass default: robots stay respected
        assert any(
            "RESPECT_ROBOTS" in rec.getMessage() or "respect_robots" in str(rec.__dict__)
            for rec in caplog.records
        )

    def test_recognized_bools_still_parse(self):
        settings = CrawlerSettings.from_mapping(
            {
                "BROWSER_CRAWLER_RESPECT_ROBOTS": "no",
                "BROWSER_CRAWLER_ADAPTIVE_THROTTLING": "1",
            }
        )
        assert settings.respect_robots is False
        assert settings.adaptive_throttling is True
