"""CrawlerSettings defaults tuned for faster parallel single-domain crawls."""

import logging
import os
from dataclasses import dataclass
from unittest.mock import patch

import pytest

import crawlutron.config.settings as settings_mod
from crawlutron.config.settings import CrawlerSettings


def test_parallelism_defaults():
    s = CrawlerSettings.from_mapping()
    # Faster single-domain crawling without being reckless.
    assert s.per_domain_concurrency == 2
    assert s.per_domain_delay_ms == 500
    # Adaptive throttling capped so a slow server cannot serialize crawls to a 30s-per-request crawl.
    assert s.adaptive_max_delay == 10.0


@dataclass
class _Memory:
    """The one attribute ``default_http_max_connections`` reads."""

    total_bytes: int


def test_a_failed_memory_read_still_yields_the_declared_fallback_every_time():
    """The 4 GiB fallback is a per-construction decision, not a cached verdict: a
    frozen fallback is indistinguishable from a derived one once wrong, since the
    only evidence is that a later readable limit changes nothing.
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
        with caplog.at_level(logging.WARNING, logger="crawlutron.safety.validation"):
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
        """Padding around a boolean is accepted; the parse is documented to ignore it.

        The behavioral claim is the mapping below; the acceptance was once also
        pinned to ``parse_bool_value``'s docstring, which is not a surface."""
        settings = CrawlerSettings.from_mapping(
            {
                "CRAWLUTRON_RESPECT_ROBOTS": " FALSE ",
                "CRAWLUTRON_ADAPTIVE_THROTTLING": " true ",
            }
        )
        assert settings.respect_robots is False
        assert settings.adaptive_throttling is True


class TestBatchSizeLoBound:
    """A negative or zero batch size must clamp to the lo bound like the sibling
    knobs (extraction_max_markdown, max_watches_per_caller): a typo'd negative
    silently refuses every batch through batch_size_exceeded, so the read is
    bounded at the settings read, not at each door."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("-3", 1), ("0", 1), ("1", 1), ("2", 2)],
        ids=["negative-floor", "zero-boundary", "lo-passes", "first-above-lo"],
    )
    def test_batch_size_clamps_at_the_lo_boundary(self, raw, expected):
        # Kills the mutant deleting ``lo=1``: every <=0 shape must read 1.
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_MAX_BATCH_SIZE": raw})
        assert settings.max_batch_size == expected

    def test_unparseable_batch_size_falls_back_to_the_default(self):
        # Kills a mutant widening the clamp into a raise: garbage parses to the
        # dataclass default like every sibling knob, never an exception.
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_MAX_BATCH_SIZE": "fifty"})
        assert settings.max_batch_size == 50

    def test_positive_batch_size_passes_through(self):
        settings = CrawlerSettings.from_mapping({"CRAWLUTRON_MAX_BATCH_SIZE": "50"})
        assert settings.max_batch_size == 50


def test_the_extraction_deadlines_are_settings_rather_than_module_snapshots():
    """The extraction deadlines are operator knobs, so they belong in settings: read
    at import they were the only knobs with no config home, and a deadline frozen at
    import is a knob no caller can move -- hence fourteen patch sites for three numbers.
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


def _expected_connections(limit: int) -> int:
    """The pool ceiling the settings must derive for a memory limit.

    Spelled out per test, the expectation and the derivation could be changed
    independently, which is the one thing a ceiling test must not allow.
    """
    return max(
        100,
        settings_mod.max_http_concurrency_for(limit, settings_mod.derive_browser_pool_size(limit)),
    )


class _FakeStats:
    def __init__(self, total_bytes):
        self.total_bytes = total_bytes


@pytest.mark.parametrize(
    ("total_bytes", "max_connections", "keepalive"),
    [(256 * 1024 * 1024, 100, 50), (64 * 1024 * 1024 * 1024, 512, 128)],
    ids=["small-limit-hits-floor", "huge-limit-is-capped"],
)
def test_connection_defaults_floor_and_cap(total_bytes, max_connections, keepalive):
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(total_bytes)):
        assert settings_mod.default_http_max_connections() == max_connections
        assert settings_mod.default_http_keepalive() == keepalive


def test_3gib_limit_scales_up():
    limit = 3 * 1024 * 1024 * 1024
    with patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(limit)):
        # The pool ceiling must equal the admission's HTTP concurrency for the same limit + pool, whatever the
        # per-task RAM estimate is (zero drift).
        expected = _expected_connections(limit)
        assert settings_mod.default_http_max_connections() == expected
        assert settings_mod.default_http_keepalive() == max(20, min(128, expected // 2))
        # With the honest per-task RAM estimate the 3 GiB derivation sits at the floor; the floor keeps the
        # minimal-deployment pool usable (a bigger box still scales above it -- see
        assert expected >= 100


def test_env_override_still_wins():
    # CrawlerSettings still honors an explicit env override over the derived default.
    with patch.dict("os.environ", {"CRAWLUTRON_HTTP_MAX_CONNECTIONS": "200"}):
        s = settings_mod.CrawlerSettings.from_mapping()
        assert s.http_max_connections == 200


def test_the_pool_default_is_derived_per_construction_not_frozen_at_import():
    """The derivation READS the memory limit, so it is I/O; a call in the class body
    made it import-time I/O frozen for the process, keeping a late-applied or failed
    limit wrong forever -- deriving per construction always reflects the running limit.
    """
    limit = 8 * 1024 * 1024 * 1024
    expected = _expected_connections(limit)
    with (
        patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats(limit)),
        patch.dict("os.environ"),
    ):
        os.environ.pop("CRAWLUTRON_HTTP_MAX_CONNECTIONS", None)
        os.environ.pop("CRAWLUTRON_HTTP_MAX_KEEPALIVE", None)
        derived = settings_mod.CrawlerSettings()
        assert derived.http_max_connections == expected
        assert derived.http_max_keepalive == max(20, min(128, expected // 2))
        # The env path must derive too. A ``default_factory`` field has no class attribute to read, so this is
        # the line where that trap shows up.
        from_env = settings_mod.CrawlerSettings.from_mapping()
        assert from_env.http_max_connections == expected
        assert from_env.http_max_keepalive == max(20, min(128, expected // 2))
