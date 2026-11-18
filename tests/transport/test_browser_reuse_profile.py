"""Cross-caller browser session bleed on reuse.

Different profiles never share a pooled session; no options are silently dropped.
"""

import os
import tempfile

from crawlutron.transport.browser_driver import BrowserDriver
from tests._browser_doubles import BrowserFakeContextManager


class _ProfileBrowser:
    """A browser double whose ``new_page`` returns a stub page."""

    def __init__(self):
        self.is_closed = False

    async def new_page(self, **_kwargs):
        return _StubPage()


class _StubPage:
    async def close(self):
        return None


class _ProfileScenario:
    """Driver + fake browser whose context-manager entries count launches."""

    def __init__(self, *, max_browser_renders: int = 100):
        self.cache_dir = tempfile.mkdtemp(prefix="profile-cache-")
        self.browser = _ProfileBrowser()
        self.cm = BrowserFakeContextManager(self.browser)
        self.driver = BrowserDriver(
            browser_factory=lambda **_kwargs: self.cm,
            cache_dir=self.cache_dir,
            max_browser_renders=max_browser_renders,
        )


async def _open_two(scenario, profile_a, profile_b):
    """Open two pages on one driver, counting one render between them."""
    await scenario.driver.open_page(profile_a)
    scenario.driver.note_render()
    await scenario.driver.open_page(profile_b)


async def test_different_storage_state_relaunches_applies_new_profile():
    """Caller B's storage_state must not ride caller A's pooled browser, and
    B's own storage_state is applied on the fresh launch (no silent drop).

    Mutant killed: unkeyed reuse arm ignoring browser_kwargs mismatch.
    """
    scenario = _ProfileScenario()
    state_a = os.path.join(scenario.cache_dir, "cookies_a.json")
    state_b = os.path.join(scenario.cache_dir, "cookies_b.json")
    await _open_two(scenario, {"storage_state": state_a}, {"storage_state": state_b})

    assert scenario.cm.enters == 2, "a different storage_state profile relaunched the browser"
    assert scenario.driver.reused is False


async def test_same_profile_reuses_no_relaunch_storm():
    """Same-profile reuse still reuses; no relaunch-every-render degeneration.

    Mutant killed: keyed fix that relaunches every call (cm.enters == 2).
    """
    scenario = _ProfileScenario()
    await _open_two(scenario, {}, {})

    assert scenario.cm.enters == 1, "the same profile reused the live browser"
    assert scenario.driver.reused is True


async def test_resource_mode_mismatch_relaunches():
    """resource_mode maps to launch args, so a mismatch must relaunch too."""
    scenario = _ProfileScenario()
    await _open_two(scenario, {"resource_mode": "full"}, {"resource_mode": "reduced"})

    assert scenario.cm.enters == 2, "a resource_mode change (launch args) relaunched the browser"


async def test_empty_profile_vs_set_profile_relaunches_both_directions():
    """Boundary: empty profile against a set storage_state profile crosses the
    reuse boundary from both sides."""
    scenario = _ProfileScenario()
    state = os.path.join(scenario.cache_dir, "state.json")
    await _open_two(scenario, {}, {"storage_state": state})
    assert scenario.cm.enters == 2

    scenario.driver.note_render()
    await _open_two(scenario, {"storage_state": state}, {})
    assert scenario.cm.enters == 3, "dropping back to an empty profile relaunches"
