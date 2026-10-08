"""Browser driver fixed overhead must be configurable via render knobs.

Lazy-scroll steps/delay and the cookie-harvest timeout are hardcoded
(3 / 1s / 5.0s). They must be policy/env-configurable with byte-identical
defaults. Ready-state/challenge behavior is confirmed and stays untouched.
"""

import tempfile

import pytest

from app.transport.browser_driver import BrowserDriver
from tests._assertions import set_optional_env
from tests._browser_doubles import ScriptedPage
from tests._policy_factories import BOOL_ENV_CASES


def _settings(**overrides):
    from app.settings import CrawlerSettings

    return CrawlerSettings(**overrides)


@pytest.mark.parametrize(
    ("profile", "expected_evaluates", "expected_sleeps"),
    [
        ({}, 1, [0.25]),
        ({"lazy_scroll_steps": 2, "lazy_scroll_delay_seconds": 0.5}, 2, [0.5, 0.5]),
    ],
)
async def test_lazy_scroll_follows_settings_or_profile(profile, expected_evaluates, expected_sleeps):
    """Settings-provided steps/delay drive the scroll; a browser_profile overrides."""
    sleeps: list[float] = []
    driver = BrowserDriver(
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_settings(browser_lazy_scroll_steps=1, browser_lazy_scroll_delay_seconds=0.25),
    )
    page = ScriptedPage(answers={})
    await driver.lazy_load_scroll(page, profile)
    assert len(page.evaluates) == expected_evaluates
    assert sleeps == expected_sleeps


async def test_lazy_scroll_zero_steps_skips_work():
    driver = BrowserDriver(
        sleep=lambda seconds: (_ for _ in ()).throw(AssertionError("must not sleep")),
        settings=_settings(browser_lazy_scroll_steps=0),
    )
    page = ScriptedPage(answers={})
    await driver.lazy_load_scroll(page)
    assert page.evaluates == []


def test_harvest_timeout_resolution_order():
    """profile -> settings -> 5.0 default; garbage falls back, never raises."""
    default = BrowserDriver().harvest_timeout_for({})
    assert default == 5.0
    via_settings = BrowserDriver(
        settings=_settings(browser_cookie_harvest_timeout_seconds=0.75)
    ).harvest_timeout_for({})
    assert via_settings == 0.75
    via_profile = BrowserDriver().harvest_timeout_for({"cookie_harvest_timeout_seconds": 1.5})
    assert via_profile == 1.5
    assert BrowserDriver().harvest_timeout_for({"cookie_harvest_timeout_seconds": "junk"}) == 5.0


def test_browser_knob_defaults_unchanged():
    """Configurability must not move the shipped defaults (policy: keep 3/1/5.0).

    Shipped defaults live in constants + settings, which nothing mutates at
    runtime: a driver reads its knobs at construction, so there is no module
    global for a test to patch and drift behind.
    """
    from app.vocabulary.limits import (
        DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS,
        DEFAULT_LAZY_SCROLL_DELAY_SECONDS,
        DEFAULT_LAZY_SCROLL_STEPS,
    )

    assert DEFAULT_LAZY_SCROLL_STEPS == 3
    assert DEFAULT_LAZY_SCROLL_DELAY_SECONDS == 1
    assert DEFAULT_COOKIE_HARVEST_TIMEOUT_SECONDS == 5.0
    plain = _settings()
    assert plain.browser_lazy_scroll_steps == 3
    assert plain.browser_lazy_scroll_delay_seconds == 1
    assert plain.browser_cookie_harvest_timeout_seconds == 5.0
    assert BrowserDriver(settings=plain).harvest_timeout_for({}) == 5.0
    assert BrowserDriver(settings=plain).scroll_config_for({}) == (3, 1)


@pytest.mark.parametrize(("raw", "expected"), BOOL_ENV_CASES)
def test_headless_decision_uses_shared_bool_parser(monkeypatch, raw, expected):
    set_optional_env(monkeypatch, "CRAWLUTRON_HEADLESS", raw)
    assert BrowserDriver().browser_kwargs({})["headless"] is expected


def test_browser_knob_env_mapping():
    """Env vars flow through CrawlerSettings; garbage falls back to defaults."""
    from app.settings import CrawlerSettings

    mapped = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_LAZY_SCROLL_STEPS": "7",
            "CRAWLUTRON_LAZY_SCROLL_DELAY_SECONDS": "0.2",
            "CRAWLUTRON_COOKIE_HARVEST_TIMEOUT_SECONDS": "2.5",
        }
    )
    assert mapped.browser_lazy_scroll_steps == 7
    assert mapped.browser_lazy_scroll_delay_seconds == 0.2
    assert mapped.browser_cookie_harvest_timeout_seconds == 2.5
    junk = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_LAZY_SCROLL_STEPS": "junk",
            "CRAWLUTRON_LAZY_SCROLL_DELAY_SECONDS": "junk",
            "CRAWLUTRON_COOKIE_HARVEST_TIMEOUT_SECONDS": "junk",
        }
    )
    assert junk.browser_lazy_scroll_steps == 3
    assert junk.browser_lazy_scroll_delay_seconds == 1
    assert junk.browser_cookie_harvest_timeout_seconds == 5.0


def test_log_knob_env_mapping():
    from app.settings import CrawlerSettings

    log_file = f"{tempfile.gettempdir()}/crawler.jsonl"
    mapped = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_LOG_FILE": log_file,
            "CRAWLUTRON_LOG_MAX_BYTES": "4096",
            "CRAWLUTRON_LOG_BACKUP_COUNT": "7",
        }
    )

    assert mapped.log_file_path == log_file
    assert mapped.log_file_max_bytes == 4096
    assert mapped.log_file_backup_count == 7
