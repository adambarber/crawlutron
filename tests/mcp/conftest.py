"""Shared fixtures for the MCP endpoint tests."""

from __future__ import annotations

import pytest

from tests._browser_doubles import FakeBrowserPool, owned_browser_pool


@pytest.fixture
def browser_pool_stub():
    """Make the browser pool present without launching a real browser.

    The crawl/map tools run the full strategy chain; the stub pool merely stops
    the ``browser_pool_unavailable`` 503 (HTTP succeeds first in the suite)."""
    with owned_browser_pool(FakeBrowserPool()):
        yield
