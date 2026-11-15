"""Bounds on env reads at the admission edge (settings.py): out-of-range knobs
refuse at construction, never silently accepted. One bound per knob, each
derived from a model, a documented limit, or a stated default.
"""

from __future__ import annotations

import logging

import pytest

from crawlutron.config.settings import CrawlerSettings

_DEFAULT_CHALLENGE_WAIT = 15  # DEFAULT_CHALLENGE_WAIT_SECONDS in vocabulary.limits


class TestHttpMaxConnectionsBounds:
    def test_below_floor_refuses_to_the_default(self, caplog):
        """-1 bricks httpx at first fetch: the read refuses and falls back to
        the RAM-derived default instead of accepting a dead pool."""
        default = CrawlerSettings.from_mapping()
        with caplog.at_level(logging.WARNING):
            settings = CrawlerSettings.from_mapping({"CRAWLUTRON_HTTP_MAX_CONNECTIONS": "-1"})
        assert settings.http_max_connections == default.http_max_connections
        refused = [r for r in caplog.records if r.message == "http_max_connections_out_of_range"]
        assert refused, "a refused value must be visible at startup"
        assert refused[0].env_name == "CRAWLUTRON_HTTP_MAX_CONNECTIONS"
        assert refused[0].env_value == "-1"

    def test_above_ceiling_refuses_to_the_default(self, caplog):
        default = CrawlerSettings.from_mapping()
        with caplog.at_level(logging.WARNING):
            settings = CrawlerSettings.from_mapping({"CRAWLUTRON_HTTP_MAX_CONNECTIONS": "9999"})
        assert settings.http_max_connections == default.http_max_connections
        assert any(r.message == "http_max_connections_out_of_range" for r in caplog.records)

    @pytest.mark.parametrize("raw", ["1", "2", "100", "512"])
    def test_within_bounds_passes_through(self, raw):
        assert CrawlerSettings.from_mapping(
            {"CRAWLUTRON_HTTP_MAX_CONNECTIONS": raw}
        ).http_max_connections == int(raw)


class TestHttpMaxKeepaliveBound:
    def test_keepalive_above_connections_clamps_loudly(self, caplog):
        """keepalive > connections is incoherent (httpx: keepalive <= max_connections);
        clamp to the ceiling loudly, naming both knobs and the bound."""
        with caplog.at_level(logging.WARNING):
            settings = CrawlerSettings.from_mapping(
                {
                    "CRAWLUTRON_HTTP_MAX_CONNECTIONS": "100",
                    "CRAWLUTRON_HTTP_MAX_KEEPALIVE": "500",
                }
            )
        assert settings.http_max_keepalive == 100  # clamped to max_connections
        loud = [r for r in caplog.records if r.message.startswith("http_max_keepalive_above_max_connections")]
        assert loud, "a clamped typo must be visible at startup"
        assert "CRAWLUTRON_HTTP_MAX_KEEPALIVE" in loud[0].message
        assert "CRAWLUTRON_HTTP_MAX_CONNECTIONS" in loud[0].message
        assert "100" in loud[0].message  # the bound value named in the message

    def test_keepalive_at_connections_passes(self):
        assert (
            CrawlerSettings.from_mapping(
                {"CRAWLUTRON_HTTP_MAX_CONNECTIONS": "100", "CRAWLUTRON_HTTP_MAX_KEEPALIVE": "100"}
            ).http_max_keepalive
            == 100
        )

    def test_keepalive_below_connections_passes(self):
        assert (
            CrawlerSettings.from_mapping(
                {"CRAWLUTRON_HTTP_MAX_CONNECTIONS": "100", "CRAWLUTRON_HTTP_MAX_KEEPALIVE": "50"}
            ).http_max_keepalive
            == 50
        )

    def test_negative_keepalive_clamps_to_zero(self):
        assert (
            CrawlerSettings.from_mapping(
                {"CRAWLUTRON_HTTP_MAX_CONNECTIONS": "100", "CRAWLUTRON_HTTP_MAX_KEEPALIVE": "-5"}
            ).http_max_keepalive
            == 0
        )


class TestHttpKeepaliveExpiryBound:
    def test_negative_expiry_clamps_to_zero(self):
        assert (
            CrawlerSettings.from_mapping({"CRAWLUTRON_HTTP_KEEPALIVE_EXPIRY": "-5.0"}).http_keepalive_expiry
            == 0.0
        )

    def test_expiry_passes_through(self):
        assert (
            CrawlerSettings.from_mapping({"CRAWLUTRON_HTTP_KEEPALIVE_EXPIRY": "120.5"}).http_keepalive_expiry
            == 120.5
        )


class TestChallengeWaitSecondsBounds:
    def test_above_ceiling_refuses_to_the_default(self, caplog):
        """100000 parks a pooled renderer for ~27.8h while the model caps at
        120s: the read refuses and falls back to the 15s default."""
        with caplog.at_level(logging.WARNING):
            settings = CrawlerSettings.from_mapping({"CRAWLUTRON_CHALLENGE_WAIT_SECONDS": "100000"})
        assert settings.challenge_wait_seconds == _DEFAULT_CHALLENGE_WAIT
        refused = [r for r in caplog.records if r.message == "challenge_wait_seconds_out_of_range"]
        assert refused, "a refused value must be visible at startup"
        assert refused[0].env_name == "CRAWLUTRON_CHALLENGE_WAIT_SECONDS"
        assert refused[0].env_value == "100000"

    def test_below_floor_refuses_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            settings = CrawlerSettings.from_mapping({"CRAWLUTRON_CHALLENGE_WAIT_SECONDS": "-5"})
        assert settings.challenge_wait_seconds == _DEFAULT_CHALLENGE_WAIT
        assert any(r.message == "challenge_wait_seconds_out_of_range" for r in caplog.records)

    @pytest.mark.parametrize("raw", ["0", "1", "15", "120"])
    def test_within_bounds_passes_through(self, raw):
        assert CrawlerSettings.from_mapping(
            {"CRAWLUTRON_CHALLENGE_WAIT_SECONDS": raw}
        ).challenge_wait_seconds == int(raw)
