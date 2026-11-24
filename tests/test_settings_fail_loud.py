"""Fail-loud settings: unknown keys refused, alias targets parsed, cache door gated."""

from __future__ import annotations

import pytest

from crawlutron.config.settings import RECOGNIZED_SETTING_KEYS, CrawlerSettings
from crawlutron.config.settings_cache import get_cached_settings, reset_cached_settings

_ALIAS_SOURCE_FIELD_FORMS = {
    "CRAWLUTRON_LOG_FILE_PATH",
    "CRAWLUTRON_LOG_FILE_MAX_BYTES",
    "CRAWLUTRON_LOG_FILE_BACKUP_COUNT",
    "CRAWLUTRON_BROWSER_LAZY_SCROLL_STEPS",
    "CRAWLUTRON_BROWSER_LAZY_SCROLL_DELAY_SECONDS",
    "CRAWLUTRON_BROWSER_COOKIE_HARVEST_TIMEOUT_SECONDS",
}

_ALIAS_TARGETS = {
    "CRAWLUTRON_LOG_FILE",
    "CRAWLUTRON_LOG_MAX_BYTES",
    "CRAWLUTRON_LOG_BACKUP_COUNT",
    "CRAWLUTRON_LAZY_SCROLL_STEPS",
    "CRAWLUTRON_LAZY_SCROLL_DELAY_SECONDS",
    "CRAWLUTRON_COOKIE_HARVEST_TIMEOUT_SECONDS",
}


def test_recognized_keys_exclude_alias_source_field_forms():
    """RECOGNIZED_SETTING_KEYS must not include alias-source field forms."""
    for key in _ALIAS_SOURCE_FIELD_FORMS:
        assert key not in RECOGNIZED_SETTING_KEYS
    for key in _ALIAS_TARGETS:
        assert key in RECOGNIZED_SETTING_KEYS


@pytest.mark.parametrize("key", sorted(_ALIAS_SOURCE_FIELD_FORMS))
def test_alias_source_field_form_refused(key):
    with pytest.raises(ValueError, match="unknown setting"):
        CrawlerSettings.from_mapping({key: "x"})


@pytest.mark.parametrize("key", sorted(_ALIAS_TARGETS))
def test_alias_target_parses(key):
    CrawlerSettings.from_mapping({key: "9"})


def test_non_crawlutron_key_refused():
    with pytest.raises(ValueError, match="unknown setting"):
        CrawlerSettings.from_mapping({"CRAWLUTRON_HOST": "0.0.0.0", "NOT_A_SETTING": "x"})


def test_empty_mapping_ok():
    CrawlerSettings.from_mapping({})


def test_none_mapping_ok():
    CrawlerSettings.from_mapping(None)


def test_environ_pass_through_does_not_raise(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_HOST", "0.0.0.0")
    CrawlerSettings.from_mapping()


def test_cached_settings_identity():
    assert get_cached_settings() is get_cached_settings()


def test_reset_forces_reparse_after_env_change(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_HOST", "127.0.0.1")
    before = get_cached_settings()
    monkeypatch.setenv("CRAWLUTRON_HOST", "0.0.0.0")
    reset_cached_settings()
    after = get_cached_settings()
    assert after is not before
    assert after.host == "0.0.0.0"


def test_reset_requires_test_flag(monkeypatch):
    monkeypatch.delenv("CRAWLUTRON_TEST_ALLOW_SETTINGS_RESET", raising=False)
    with pytest.raises(RuntimeError, match="test-only"):
        reset_cached_settings()
