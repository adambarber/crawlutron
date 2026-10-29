"""The browser renderer's recycle accounting must count every render attempt.

``note_render`` is the driver's recycle counter; a render that raises between
page-open and the guarded body must still count one (#223).
"""

from __future__ import annotations

import pytest

from crawlutron.models.render_options import RenderOptions
from tests._browser_doubles import FakeBrowserDriver, build_pinned_renderer, stub_pinned_request
from tests._browser_page_doubles import BrowserFakePage, FakePageContext
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


class _RouteRaisesPage(BrowserFakePage):
    """A page whose CONTEXT route setup fails the moment the renderer wires the guard."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        class _Raising(FakePageContext):
            async def route(self, pattern, handler):
                raise RuntimeError("route setup failed")

        self.context = _Raising()


class _CountingDriver(FakeBrowserDriver):
    """The duck-typed driver, recording how many renders it is asked to count."""

    def __init__(self, page, **kwargs):
        super().__init__(page, **kwargs)
        self.render_count = 0

    def note_render(self):
        self.render_count += 1


async def test_route_raise_after_page_open_still_counts_recycle():
    """A raise between page-open and the try must not skip the recycle accounting."""
    page = _RouteRaisesPage()
    driver = _CountingDriver(page)
    renderer = build_pinned_renderer(
        page,
        driver=driver,
        pin=stub_pinned_request(),
    )
    with pytest.raises(RuntimeError, match="route setup failed"):
        await renderer.render_once(
            canonical("http://example.com/"),
            RenderOptions(browser_profile={}),
            policy=UNRESTRICTED_HOST_POLICY,
        )
    assert driver.render_count == 1, "a render that raised after page-open still counted a recycle"
