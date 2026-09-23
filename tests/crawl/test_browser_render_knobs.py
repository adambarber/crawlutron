"""Browser renderer fixed overhead must be configurable via render knobs.

Lazy-scroll steps/delay and the cookie-harvest timeout are hardcoded
(3 / 1s / 5.0s). They must be policy/env-configurable with byte-identical
defaults. Ready-state/challenge behavior is confirmed and stays untouched.
"""

import pytest
from _testkit import BOOL_ENV_CASES, set_optional_env

from app.crawl.strategies.browser import BrowserRenderer


class _FakePage:
    def __init__(self):
        self.evaluates = 0

    async def evaluate(self, script):
        self.evaluates += 1
        return None


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
    renderer = BrowserRenderer(
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_settings(browser_lazy_scroll_steps=1, browser_lazy_scroll_delay_seconds=0.25),
    )
    page = _FakePage()
    await renderer._lazy_load_scroll(page, profile)
    assert page.evaluates == expected_evaluates
    assert sleeps == expected_sleeps


async def test_lazy_scroll_zero_steps_skips_work():
    renderer = BrowserRenderer(
        sleep=lambda seconds: (_ for _ in ()).throw(AssertionError("must not sleep")),
        settings=_settings(browser_lazy_scroll_steps=0),
    )
    page = _FakePage()
    await renderer._lazy_load_scroll(page)
    assert page.evaluates == 0


def test_harvest_timeout_resolution_order():
    """profile -> settings -> 5.0 default; garbage falls back, never raises."""
    default = BrowserRenderer()._harvest_timeout_for({})
    assert default == 5.0
    via_settings = BrowserRenderer(
        settings=_settings(browser_cookie_harvest_timeout_seconds=0.75)
    )._harvest_timeout_for({})
    assert via_settings == 0.75
    via_profile = BrowserRenderer()._harvest_timeout_for({"cookie_harvest_timeout_seconds": 1.5})
    assert via_profile == 1.5
    assert BrowserRenderer()._harvest_timeout_for({"cookie_harvest_timeout_seconds": "junk"}) == 5.0


def test_browser_knob_defaults_unchanged():
    """Configurability must not move the shipped defaults (policy: keep 3/1/5.0).

    The module globals are deliberately NOT asserted here: the session
    ``_disable_lazy_scroll`` fixture owns them at runtime. Shipped defaults
    live in constants + settings, which no fixture mutates.
    """
    from app.constants import (
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
    assert BrowserRenderer(settings=plain)._harvest_timeout_for({}) == 5.0
    assert BrowserRenderer(settings=plain)._scroll_config_for({}) == (3, 1)


@pytest.mark.parametrize(("raw", "expected"), BOOL_ENV_CASES)
def test_headless_decision_uses_shared_bool_parser(monkeypatch, raw, expected):
    set_optional_env(monkeypatch, "BROWSER_CRAWLER_HEADLESS", raw)
    assert BrowserRenderer()._get_browser_kwargs({})["headless"] is expected


def test_browser_knob_env_mapping():
    """Env vars flow through CrawlerSettings; garbage falls back to defaults."""
    from app.settings import CrawlerSettings

    mapped = CrawlerSettings.from_mapping(
        {
            "BROWSER_CRAWLER_LAZY_SCROLL_STEPS": "7",
            "BROWSER_CRAWLER_LAZY_SCROLL_DELAY_SECONDS": "0.2",
            "BROWSER_CRAWLER_COOKIE_HARVEST_TIMEOUT_SECONDS": "2.5",
        }
    )
    assert mapped.browser_lazy_scroll_steps == 7
    assert mapped.browser_lazy_scroll_delay_seconds == 0.2
    assert mapped.browser_cookie_harvest_timeout_seconds == 2.5
    junk = CrawlerSettings.from_mapping(
        {
            "BROWSER_CRAWLER_LAZY_SCROLL_STEPS": "junk",
            "BROWSER_CRAWLER_LAZY_SCROLL_DELAY_SECONDS": "junk",
            "BROWSER_CRAWLER_COOKIE_HARVEST_TIMEOUT_SECONDS": "junk",
        }
    )
    assert junk.browser_lazy_scroll_steps == 3
    assert junk.browser_lazy_scroll_delay_seconds == 1
    assert junk.browser_cookie_harvest_timeout_seconds == 5.0
