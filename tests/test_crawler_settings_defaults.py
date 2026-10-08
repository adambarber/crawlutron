"""CrawlerSettings defaults tuned for faster parallel single-domain crawls."""

import logging
import os
from dataclasses import dataclass
from unittest.mock import patch

import app.config.settings as settings_mod
from app.config.env import parse_bool_value
from app.config.settings import CrawlerSettings


def test_parallelism_defaults():
    s = CrawlerSettings.from_mapping()
    # Faster single-domain crawling without being reckless.
    assert s.per_domain_concurrency == 2
    assert s.per_domain_delay_ms == 500
    # Adaptive throttling capped so a slow server cannot serialize crawls to
    # a 30s-per-request crawl.
    assert s.adaptive_max_delay == 10.0


@dataclass
class _Memory:
    """The one attribute ``_default_http_max_connections`` reads."""

    total_bytes: int


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
        # From_mapping used to read these off the class, which a default_factory
        # field does not have -- so it has to call the same factory.
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("CRAWLUTRON_HTTP_MAX_CONNECTIONS", None)
            os.environ.pop("CRAWLUTRON_HTTP_MAX_KEEPALIVE", None)
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
    """From_mapping must absorb garbage env values, never crash, and
    never fail a policy open on a typo'd boolean."""

    def test_garbage_numeric_values_fall_back_to_defaults(self, caplog):
        settings = CrawlerSettings.from_mapping(
            {
                "CRAWLUTRON_GLOBAL_CONCURRENCY": "four",
                "CRAWLUTRON_ADAPTIVE_MIN_DELAY": "not-a-float",
            }
        )
        assert not hasattr(settings, "host_port")
        assert settings.global_concurrency == 4
        assert settings.adaptive_min_delay == 0.5

    def test_typo_respect_robots_fails_closed_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.safety.validation"):
            settings = CrawlerSettings.from_mapping({"CRAWLUTRON_RESPECT_ROBOTS": "ture"})
        assert settings.respect_robots is True  # dataclass default: robots stay respected
        assert any(
            "RESPECT_ROBOTS" in rec.getMessage() or "respect_robots" in str(rec.__dict__)
            for rec in caplog.records
        )

    def test_recognized_bools_still_parse(self):
        settings = CrawlerSettings.from_mapping(
            {
                "CRAWLUTRON_RESPECT_ROBOTS": "no",
                "CRAWLUTRON_ADAPTIVE_THROTTLING": "1",
            }
        )
        assert settings.respect_robots is False
        assert settings.adaptive_throttling is True

    def test_padded_boolean_is_intentionally_accepted_and_declared(self):
        assert "surrounding whitespace" in (parse_bool_value.__doc__ or "").lower()
        settings = CrawlerSettings.from_mapping(
            {
                "CRAWLUTRON_RESPECT_ROBOTS": " FALSE ",
                "CRAWLUTRON_ADAPTIVE_THROTTLING": " true ",
            }
        )
        assert settings.respect_robots is False
        assert settings.adaptive_throttling is True


def test_the_extraction_deadlines_are_settings_rather_than_module_snapshots():
    """The extraction deadlines are operator knobs, so they belong in settings.

    Each of the three was read from its environment variable by ``extraction.py``
    at import and kept as a module-private constant. That made them the only
    knobs in that module with no home in the config -- ``EXTRACTION_DOC_MAX_INPUT_BYTES``
    sits ten lines above them and is public for exactly the same reason. And a
    deadline frozen at import is a knob no caller can move: a test could only
    reach it by rewriting a private module global, which is why fourteen patch
    sites existed for three numbers.

    The browser deadlines were centralized here for the same reason and carry
    that in their comment. These are the remaining three in the same category.
    """
    defaults = CrawlerSettings.from_mapping()
    assert defaults.extraction_doc_timeout == 10.0
    assert defaults.extraction_selector_timeout == 2.0
    assert defaults.extraction_doc_acquire_timeout == 90.0

    with patch.dict(
        os.environ,
        {
            "CRAWLUTRON_EXTRACTION_DOC_TIMEOUT": "0.25",
            "CRAWLUTRON_EXTRACTION_SELECTOR_TIMEOUT": "0.5",
            "CRAWLUTRON_EXTRACTION_DOC_ACQUIRE_TIMEOUT": "1.5",
        },
        clear=False,
    ):
        configured = CrawlerSettings.from_mapping()
    assert configured.extraction_doc_timeout == 0.25
    assert configured.extraction_selector_timeout == 0.5
    assert configured.extraction_doc_acquire_timeout == 1.5
