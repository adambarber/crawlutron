"""Lazy-scroll stops early when the page height stops growing (#356)."""

from crawlutron.transport.browser_driver import BrowserDriver
from tests._browser_page_doubles import ScriptedPage


def _driver(sleeps: list[float], steps: int = 5) -> BrowserDriver:
    return BrowserDriver(
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_settings(steps),
    )


def _settings(steps: int):
    from crawlutron.config.settings import CrawlerSettings

    return CrawlerSettings(browser_lazy_scroll_steps=steps, browser_lazy_scroll_delay_seconds=0.25)


async def test_a_static_page_skips_the_remaining_waits():
    """A page that never grows: the loop stops after two stable polls instead
    of sleeping out every step -- the fixed-delay tax on static sites."""
    sleeps: list[float] = []
    page = ScriptedPage(answers={"scrollHeight": 1000})
    await _driver(sleeps).lazy_load_scroll(page)
    assert len(sleeps) == 2, sleeps


async def test_a_growing_page_uses_every_step():
    """A page still growing at the last step is not cut short: each scroll
    returns a taller height, so the loop runs the full step budget."""
    sleeps: list[float] = []
    page = ScriptedPage(answers={})
    heights = iter(range(100, 100 + 10 * 100, 100))

    async def evaluate(expression, *args, **kwargs):
        page.evaluates.append(expression)
        return next(heights, None)

    object.__setattr__(page, "evaluate", evaluate)
    await _driver(sleeps).lazy_load_scroll(page)
    assert len(sleeps) == 5


async def test_an_unreadable_height_keeps_the_legacy_loop():
    """A page that answers no height (the double's "" default) must keep the
    legacy fixed loop: no signal, no early exit, all steps slept."""
    sleeps: list[float] = []
    page = ScriptedPage(answers={})
    await _driver(sleeps).lazy_load_scroll(page)
    assert len(sleeps) == 5


async def test_a_scroll_failure_still_returns():
    """The scroll evaluate raising stays best-effort: return, no sleep."""
    sleeps: list[float] = []
    page = ScriptedPage(answers={})

    async def evaluate(expression, *args, **kwargs):
        raise RuntimeError("page closed")

    object.__setattr__(page, "evaluate", evaluate)
    await _driver(sleeps).lazy_load_scroll(page)
    assert sleeps == []


async def test_zero_steps_still_skip_everything():
    sleeps: list[float] = []
    driver = BrowserDriver(
        sleep=lambda seconds: sleeps.append(seconds),
        settings=_settings(0),
    )
    page = ScriptedPage(answers={"scrollHeight": 5})
    await driver.lazy_load_scroll(page)
    assert page.evaluates == []
    assert sleeps == []
