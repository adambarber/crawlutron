"""CrawlerSettings defaults tuned for faster parallel single-domain crawls (#7)."""

import logging
from unittest.mock import patch

import app.settings as settings_mod
from app.settings import CrawlerSettings
from app.validation import parse_bool_value


def test_parallelism_defaults():
    s = CrawlerSettings.from_mapping()
    # #7: faster single-domain crawling without being reckless.
    assert s.per_domain_concurrency == 2
    assert s.per_domain_delay_ms == 500
    # Adaptive throttling capped so a slow server cannot serialize crawls to
    # a 30s-per-request crawl.
    assert s.adaptive_max_delay == 10.0


class _Memory:
    """The one attribute ``_default_http_max_connections`` reads."""

    def __init__(self, total_bytes: int):
        self.total_bytes = total_bytes


def test_the_pool_default_is_derived_per_construction_not_frozen_at_import():
    """The pool ceiling is RAM-derived, so it must be read when settings are built.

    Both pool fields used to call their derivation as a default value in the
    dataclass body, which evaluated it ONCE -- at import. The derivation reads
    the cgroup memory limit, and falls back to 4 GiB when that read raises, so a
    process whose limit only became readable after the first import (or whose
    read failed) kept that number for its whole life, with nothing in the
    config able to explain the pool it was running.
    """
    limit = 8 * 1024 * 1024 * 1024
    with patch.object(settings_mod, "read_memory_stats", lambda: _Memory(limit)):
        expected = max(
            100,
            settings_mod.max_http_concurrency_for(limit, settings_mod.derive_browser_pool_size(limit)),
        )
        built = CrawlerSettings()
        assert built.http_max_connections == expected
        assert built.http_max_keepalive == max(20, min(128, expected // 2))
        # from_mapping used to read these off the class, which a default_factory
        # field does not have -- so it has to call the same factory.
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("BROWSER_CRAWLER_HTTP_MAX_CONNECTIONS", None)
            os.environ.pop("BROWSER_CRAWLER_HTTP_MAX_KEEPALIVE", None)
            from_env = CrawlerSettings.from_mapping()
        assert from_env.http_max_connections == expected


def test_a_failed_memory_read_still_yields_the_declared_fallback_every_time():
    """The 4 GiB fallback is a per-construction decision, not a cached verdict.

    Pinned because a frozen fallback is indistinguishable from a derived one
    once it is wrong: the only evidence is that a later, readable limit does
    not change the number.
    """
    with patch.object(settings_mod, "read_memory_stats", lambda: _Memory(64 * 1024**3)):
        derived = CrawlerSettings().http_max_connections
    with patch.object(settings_mod, "read_memory_stats", side_effect=OSError("no cgroup")):
        fell_back = CrawlerSettings().http_max_connections
    assert derived != fell_back, "both paths produced the same pool; the read is not happening"


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

    def test_padded_boolean_is_intentionally_accepted_and_declared(self):
        assert "surrounding whitespace" in (parse_bool_value.__doc__ or "").lower()
        settings = CrawlerSettings.from_mapping(
            {
                "BROWSER_CRAWLER_RESPECT_ROBOTS": " FALSE ",
                "BROWSER_CRAWLER_ADAPTIVE_THROTTLING": " true ",
            }
        )
        assert settings.respect_robots is False
        assert settings.adaptive_throttling is True
