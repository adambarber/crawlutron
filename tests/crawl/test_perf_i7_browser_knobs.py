"""Perf item 7 (RED): browser fixed overhead must be configurable, defaults fixed.

Lazy-scroll steps/delay and the cookie-harvest timeout are hardcoded
(3 / 1s / 5.0s). They must be policy/env-configurable with byte-identical
defaults. Ready-state/challenge behavior is confirmed and stays untouched.
"""

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


async def test_lazy_scroll_uses_settings_values():
    """Settings-provided steps/delay drive the scroll (not the 3 x 1s consts)."""
    sleeps: list[float] = []
    renderer = BrowserRenderer(
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_settings(browser_lazy_scroll_steps=1, browser_lazy_scroll_delay_seconds=0.25),
    )
    page = _FakePage()
    await renderer._lazy_load_scroll(page)
    assert page.evaluates == 1
    assert sleeps == [0.25]


async def test_lazy_scroll_profile_overrides_settings():
    """Per-request browser_profile (policy) beats the renderer settings."""
    sleeps: list[float] = []
    renderer = BrowserRenderer(
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_settings(browser_lazy_scroll_steps=1, browser_lazy_scroll_delay_seconds=0.25),
    )
    page = _FakePage()
    await renderer._lazy_load_scroll(page, {"lazy_scroll_steps": 2, "lazy_scroll_delay_seconds": 0.5})
    assert page.evaluates == 2
    assert sleeps == [0.5, 0.5]


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
