"""Bounds on env reads at the admission edge (settings.py): out-of-range knobs
refuse at construction, never silently accepted. One bound per knob, each
derived from a model, a documented limit, or a stated default.
"""

from __future__ import annotations

import logging

import pytest

import crawlutron.config.settings as settings_mod
from crawlutron.config.settings import CrawlerSettings

_DEFAULT_CHALLENGE_WAIT = 15  # DEFAULT_CHALLENGE_WAIT_SECONDS in vocabulary.limits


class TestHttpMaxConnectionsBounds:
    """httpx needs a pool able to open at least one connection; <= 0 bricks
    the client at first fetch. 512 is the pool cap the README and .env.example
    document, equal to the admission layer's HTTP concurrency ceiling."""

    @pytest.mark.parametrize(
        ("raw", "expect_refusal"),
        [
            ("0", True),
            ("1", False),
            ("512", False),
            ("513", True),
        ],
        ids=["conn-lo-refused", "conn-lo", "conn-hi", "conn-hi-refused"],
    )
    def test_boundary_table(self, raw, expect_refusal, caplog):
        default = CrawlerSettings.from_mapping()
        with caplog.at_level(logging.WARNING):
            settings = CrawlerSettings.from_mapping({"CRAWLUTRON_HTTP_MAX_CONNECTIONS": raw})
        if expect_refusal:
            assert settings.http_max_connections == default.http_max_connections
            refused = [r for r in caplog.records if r.message == "http_max_connections_out_of_range"]
            assert refused, "a refused value must be visible at startup"
            assert refused[0].env_name == "CRAWLUTRON_HTTP_MAX_CONNECTIONS"
            assert refused[0].env_value == raw
        else:
            assert settings.http_max_connections == int(raw)


class TestHttpMaxKeepaliveBound:
    """httpx documents keepalive <= max_connections but does not enforce it;
    an explicit keepalive above the ceiling is refused, never clamped; the
    negative floor clamps to 0 intentionally."""

    @pytest.mark.parametrize(
        ("keepalive", "expect_refusal"),
        [
            ("-1", True),
            ("0", False),
            ("100", False),
            ("101", True),
        ],
        ids=["keepalive-lo-refused", "keepalive-lo", "keepalive-hi", "keepalive-hi-refused"],
    )
    def test_boundary_table(self, keepalive, expect_refusal, caplog):
        with caplog.at_level(logging.WARNING):
            settings = CrawlerSettings.from_mapping(
                {
                    "CRAWLUTRON_HTTP_MAX_CONNECTIONS": "100",
                    "CRAWLUTRON_HTTP_MAX_KEEPALIVE": keepalive,
                }
            )
        expected = min(settings_mod.default_http_keepalive(), 100) if expect_refusal else int(keepalive)
        assert settings.http_max_keepalive == expected
        if expect_refusal:
            refused = [r for r in caplog.records if r.message == "http_max_keepalive_above_max_connections"]
            assert refused, "a refused keepalive must be visible at startup"
            assert refused[0].env_name == "CRAWLUTRON_HTTP_MAX_KEEPALIVE"
            assert refused[0].env_value == keepalive

    def test_keepalive_below_connections_passes(self):
        assert (
            CrawlerSettings.from_mapping(
                {"CRAWLUTRON_HTTP_MAX_CONNECTIONS": "100", "CRAWLUTRON_HTTP_MAX_KEEPALIVE": "50"}
            ).http_max_keepalive
            == 50
        )


class TestHttpKeepaliveExpiryBound:
    """An idle keep-alive time limit cannot be negative; no ceiling is
    documented, so none is invented. The negative floor clamps to 0.0
    intentionally (a negative expiry is meaningless)."""

    def test_negative_expiry_clamps_to_zero(self):
        assert (
            CrawlerSettings.from_mapping({"CRAWLUTRON_HTTP_KEEPALIVE_EXPIRY": "-5.0"}).http_keepalive_expiry
            == 0.0
        )

    def test_zero_expiry_passes(self):
        assert (
            CrawlerSettings.from_mapping({"CRAWLUTRON_HTTP_KEEPALIVE_EXPIRY": "0.0"}).http_keepalive_expiry
            == 0.0
        )

    def test_expiry_passes_through(self):
        assert (
            CrawlerSettings.from_mapping({"CRAWLUTRON_HTTP_KEEPALIVE_EXPIRY": "120.5"}).http_keepalive_expiry
            == 120.5
        )


class TestChallengeWaitSecondsBounds:
    """100000 parks a pooled renderer for ~27.8h while the model caps at 120s;
    the read refuses and falls back to the 15s default. Bounds are derived
    from CrawlPolicyLimits.model_fields, not retyped literals."""

    @pytest.mark.parametrize(
        ("raw", "expect_refusal"),
        [
            ("-5", True),
            ("0", False),
            ("120", False),
            ("121", True),
        ],
        ids=["challenge-lo-refused", "challenge-lo", "challenge-hi", "challenge-hi-refused"],
    )
    def test_boundary_table(self, raw, expect_refusal, caplog):
        with caplog.at_level(logging.WARNING):
            settings = CrawlerSettings.from_mapping({"CRAWLUTRON_CHALLENGE_WAIT_SECONDS": raw})
        expected = _DEFAULT_CHALLENGE_WAIT if expect_refusal else int(raw)
        assert settings.challenge_wait_seconds == expected
        if expect_refusal:
            refused = [r for r in caplog.records if r.message == "challenge_wait_seconds_out_of_range"]
            assert refused, "a refused value must be visible at startup"
            assert refused[0].env_name == "CRAWLUTRON_CHALLENGE_WAIT_SECONDS"
            assert refused[0].env_value == raw
