"""CrawlerSettings defaults tuned for faster parallel single-domain crawls (#7)."""

from app.settings import CrawlerSettings


def test_parallelism_defaults():
    s = CrawlerSettings.from_mapping()
    # #7: faster single-domain crawling without being reckless.
    assert s.per_domain_concurrency == 2
    assert s.per_domain_delay_ms == 500
    # Adaptive throttling capped so a slow server cannot serialize crawls to
    # a 30s-per-request crawl.
    assert s.adaptive_max_delay == 10.0
