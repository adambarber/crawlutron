"""Runtime policy snapshot: build_runtime_policy produces a read-only typed
snapshot and settings repr never leaks secrets.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron_rest.policy import build_runtime_policy


def test_settings_repr_does_not_leak_secrets():
    """repr(settings) must not contain any secret value."""
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_TAVILY_API_KEY": "tvly-secret-123",
            "CRAWLUTRON_KEY_ADMIN_TOKEN": "admin-secret-456",
        }
    )
    rendered = repr(settings)
    assert "tvly-secret-123" not in rendered
    assert "admin-secret-456" not in rendered


def test_build_runtime_policy_maps_settings_fields():
    """build_runtime_policy copies every snapshot field from settings."""
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_SEARCH_PROVIDER": "tavily",
            "CRAWLUTRON_TAVILY_API_KEY": "tvly-secret-123",
            "CRAWLUTRON_SEARCH_LIMIT_DEFAULT": "10",
            "CRAWLUTRON_DEFAULT_TIMEOUT_SECONDS": "20",
        }
    )
    policy = build_runtime_policy(settings)
    assert policy.search_provider == "tavily"
    assert policy.tavily_api_key == "tvly-secret-123"
    assert policy.search_limit_default == 10
    assert policy.default_timeout_seconds == 20


def test_runtime_policy_is_frozen_read_only():
    """RuntimePolicy is frozen: direct assignment raises FrozenInstanceError."""
    settings = CrawlerSettings.from_mapping({})
    policy = build_runtime_policy(settings)
    attr = "search_provider"
    with pytest.raises(FrozenInstanceError):
        setattr(policy, attr, "brave")


def test_runtime_policy_repr_does_not_leak_secrets():
    """repr(snapshot) must not echo secret values."""
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_TAVILY_API_KEY": "tvly-secret-123",
            "CRAWLUTRON_KEY_ADMIN_TOKEN": "admin-secret-456",
        }
    )
    rendered = repr(build_runtime_policy(settings))
    assert "tvly-secret-123" not in rendered
    assert "admin-secret-456" not in rendered
